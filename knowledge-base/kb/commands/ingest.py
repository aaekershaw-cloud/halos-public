"""Ingest commands - add source materials to knowledge base"""

import os
import re
import json
import uuid
import hashlib
import logging
import frontmatter
from datetime import datetime
from pathlib import Path
from kb.config import get_project_root
from kb.db import get_connection
from kb.file_detect import detect as _detect_file

logger = logging.getLogger(__name__)


def _slugify(text: str) -> str:
    """Convert text to URL-safe slug."""
    slug = text.lower().strip()
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'[\s_]+', '-', slug)
    slug = re.sub(r'-+', '-', slug)
    return slug.strip('-')


def _extract_title(doc, filepath: Path) -> str:
    """Extract title from frontmatter, first heading, or filename."""
    # Try frontmatter
    if doc.metadata.get('title'):
        return doc.metadata['title']

    # Try first markdown heading
    for line in doc.content.splitlines():
        line = line.strip()
        if line.startswith('# '):
            return line[2:].strip()

    # Fall back to filename
    return filepath.stem.replace('-', ' ').replace('_', ' ').title()


def cmd_ingest_file(filepath: str, classification: str, force: bool = False,
                    direct: bool = False, agent: str = None):
    """
    Ingest a local file into knowledge base.

    Args:
        filepath: Path to file to ingest
        classification: public|internal|confidential
        force: Force ingest even if PII detected (Phase 1: always True, no PII scanning)
        direct: Write directly to wiki/ and index in FTS (skip raw→compile pipeline)
        agent: Agent scope (e.g. 'alpha', 'beta'). Omit for shared articles.
    """
    filepath = Path(filepath).expanduser().resolve()

    if not filepath.exists():
        print(f"✗ File not found: {filepath}")
        return

    print(f"Ingesting file: {filepath.name}")
    print(f"Classification: {classification}")
    if agent:
        print(f"Agent scope: {agent}")

    detection = _detect_file(filepath)
    print(f"Detected: {detection['label']} "
          f"({detection['mime'] or '—'}, "
          f"score={detection['score']:.2f}, "
          f"via {detection['source']})")

    if detection['kind'] == 'pdf':
        print("Routing to PDF extractor")
        cmd_ingest_pdf(str(filepath), classification)
        return

    if detection['kind'] == 'binary':
        print(f"✗ Skipping non-text file ({detection['label']}). "
              f"Use a dedicated ingest command (e.g. pdf) or convert to text first.")
        return

    # Read file content
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        print(f"✗ Error reading file: {e}")
        return

    if direct:
        _ingest_direct(filepath, content, classification, agent=agent, detection=detection)
    else:
        _ingest_raw(filepath, content, classification, agent=agent, detection=detection)


def _ingest_direct(filepath: Path, content: str, classification: str, agent: str = None,
                   detection: dict = None):
    """Write directly to wiki/ and index in FTS — immediately searchable."""
    from kb.search import update_article_fts

    root = get_project_root()
    doc = frontmatter.loads(content)

    # Extract metadata
    title = _extract_title(doc, filepath)
    slug = _slugify(title)
    tags = doc.metadata.get('tags', [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(',')]

    article_id = str(uuid.uuid4())
    checksum = hashlib.sha256(content.encode('utf-8')).hexdigest()

    # Prepare wiki destination
    wiki_dir = root / 'wiki' / classification
    wiki_dir.mkdir(parents=True, exist_ok=True)

    dest_path = wiki_dir / f"{slug}.md"
    content_path = str(dest_path.relative_to(root))

    # Build frontmatter
    doc.metadata.update({
        'id': article_id,
        'title': title,
        'slug': slug,
        'tags': tags,
        'classification': classification,
        'ingested_at': datetime.now().isoformat(),
    })
    if agent:
        doc.metadata['agent_scope'] = agent
    if detection:
        doc.metadata['content_type'] = detection['label']
        if detection.get('mime'):
            doc.metadata['mime_type'] = detection['mime']

    file_content = frontmatter.dumps(doc)

    # Write to temp file first (same dir for atomic rename)
    import tempfile
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(wiki_dir), suffix='.md.tmp')

    try:
        with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
            f.write(file_content)

        # Insert into articles table and FTS index
        conn = get_connection()
        conn.execute("BEGIN IMMEDIATE")

        # Handle slug collision via DB unique constraint
        conn.execute("""
            INSERT INTO articles (id, title, slug, content_path, classification, tags, checksum, agent_scope, last_confirmed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            article_id, title, slug, content_path,
            classification, json.dumps(tags), checksum, agent
        ))

        # Atomic rename temp → final
        os.replace(tmp_path, str(dest_path))
        tmp_path = None

        update_article_fts(conn, article_id)

        conn.execute("COMMIT")

        print(f"✓ Direct ingested: {slug}.md")
        print(f"  ID: {article_id}")
        print(f"  Title: {title}")
        print(f"  Path: {content_path}")
        if tags:
            print(f"  Tags: {', '.join(tags)}")
        print(f"  Status: immediately searchable")

        # Trigger ingest hook for auto-linking
        try:
            from kb.hooks.event_bus import get_bus
            bus = get_bus()
            bus.emit('ingest',
                article_id=article_id,
                content=doc.content,
                title=title,
                slug=slug,
                classification=classification,
                tags=tags,
            )
        except Exception as e:
            logger.warning(f"Ingest hook failed (non-blocking): {e}")

    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"✗ Database error: {e}")
        # Clean up temp file if rename didn't happen
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _ingest_raw(filepath: Path, content: str, classification: str, agent: str = None,
                detection: dict = None):
    """Standard ingest to raw/ — requires compile step to become searchable.

    Note: agent_scope is stored in frontmatter but only applied to articles table
    during the compile step.
    """
    checksum = hashlib.sha256(content.encode('utf-8')).hexdigest()
    file_id = str(uuid.uuid4())

    root = get_project_root()
    dest_dir = root / 'raw' / classification
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_filename = f"{file_id}{filepath.suffix}"
    dest_path = dest_dir / dest_filename

    doc = frontmatter.loads(content)
    doc.metadata.update({
        'id': file_id,
        'source_file': str(filepath),
        'classification': classification,
        'ingested_at': datetime.now().isoformat(),
        'checksum': checksum
    })
    if agent:
        doc.metadata['agent_scope'] = agent
    if detection:
        doc.metadata['content_type'] = detection['label']
        if detection.get('mime'):
            doc.metadata['mime_type'] = detection['mime']

    with open(dest_path, 'w', encoding='utf-8') as f:
        f.write(frontmatter.dumps(doc))

    conn = get_connection()
    try:
        conn.execute("BEGIN")

        conn.execute("""
            INSERT INTO raw_files (id, filename, path, classification, checksum, source_url)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            file_id,
            filepath.name,
            str(dest_path.relative_to(root)),
            classification,
            checksum,
            str(filepath)
        ))

        conn.execute("COMMIT")

        print(f"✓ Ingested: {dest_filename}")
        print(f"  ID: {file_id}")
        print(f"  Path: {dest_path.relative_to(root)}")

    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"✗ Database error: {e}")
        if dest_path.exists():
            dest_path.unlink()
        raise


def cmd_ingest_dir(dirpath: str, classification: str, direct: bool = True,
                   recursive: bool = True, ext: tuple = ('.md',),
                   dry_run: bool = False, auto: bool = False):
    """
    Batch ingest all matching files in a directory.

    Args:
        dirpath: Path to directory
        classification: public|internal|confidential
        direct: Write directly to wiki + FTS (default: True)
        recursive: Walk subdirectories (default: True)
        ext: File extensions to include (default: .md only)
        dry_run: List files without ingesting
        auto: Detect file type by content (magika) and include any text/pdf file.
              When True, `ext` is ignored.
    """
    dirpath = Path(dirpath).expanduser().resolve()

    if not dirpath.exists():
        print(f"✗ Directory not found: {dirpath}")
        return

    if not dirpath.is_dir():
        print(f"✗ Not a directory: {dirpath}")
        return

    # Collect files
    if recursive:
        candidates = [p for p in dirpath.rglob('*') if p.is_file()]
    else:
        candidates = [p for p in dirpath.iterdir() if p.is_file()]

    # Filter: skip hidden/.gitkeep; then either match extensions or content-detect
    visible = [
        p for p in sorted(candidates)
        if not p.name.startswith('.') and p.name != '.gitkeep'
    ]

    if auto:
        files = [p for p in visible if _detect_file(p)['kind'] in ('text', 'pdf')]
    else:
        files = [p for p in visible if p.suffix.lower() in ext]

    if not files:
        if auto:
            print(f"No ingestible (text/pdf) files found in {dirpath}")
        else:
            print(f"No {'/'.join(ext)} files found in {dirpath}")
        return

    mode = 'direct (immediately searchable)' if direct else 'raw (requires compile)'
    filter_desc = 'auto-detect (magika)' if auto else f"ext={'/'.join(ext)}"
    print(f"Found {len(files)} file(s) in {dirpath}")
    print(f"Classification: {classification} | Mode: {mode} | Filter: {filter_desc}")
    if dry_run:
        print("Dry run — no files will be ingested:\n")
        for f in files:
            print(f"  {f.relative_to(dirpath)}")
        return

    print()
    ok, skipped, failed = [], [], []

    for filepath in files:
        try:
            # Check if already indexed by path to avoid duplicates
            conn = get_connection()
            root = get_project_root()
            try:
                rel = str(filepath.relative_to(root))
            except ValueError:
                rel = None

            if rel:
                row = conn.execute(
                    "SELECT slug FROM articles WHERE content_path = ?", (rel,)
                ).fetchone()
                if row:
                    print(f"  ~ skip (already indexed): {filepath.name}")
                    skipped.append(filepath)
                    continue

            cmd_ingest_file(str(filepath), classification, force=False, direct=direct)
            ok.append(filepath)
        except Exception as e:
            print(f"  ✗ failed: {filepath.name} — {e}")
            failed.append(filepath)
        print()

    # Summary
    print(f"{'─' * 40}")
    print(f"Done: {len(ok)} ingested, {len(skipped)} skipped, {len(failed)} failed")
    if failed:
        print("Failed files:")
        for f in failed:
            print(f"  {f}")


def cmd_ingest_url(url: str, classification: str):
    """
    Ingest content from URL.

    Downloads content and stores as markdown.

    Args:
        url: URL to fetch
        classification: public|internal|confidential
    """
    print(f"Fetching: {url}")
    print(f"Classification: {classification}")

    try:
        import requests
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        content = response.text

    except Exception as e:
        print(f"✗ Error fetching URL: {e}")
        return

    # Calculate checksum
    checksum = hashlib.sha256(content.encode('utf-8')).hexdigest()

    # Generate unique ID
    file_id = str(uuid.uuid4())

    # Store in raw/{classification}/
    root = get_project_root()
    dest_dir = root / 'raw' / classification
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Create filename from URL
    from urllib.parse import urlparse
    parsed = urlparse(url)
    url_path = parsed.path.strip('/').replace('/', '-')
    if not url_path:
        url_path = parsed.netloc.replace('.', '-')

    dest_filename = f"{file_id}-{url_path}.md"
    dest_path = dest_dir / dest_filename

    # Create document with frontmatter
    doc = frontmatter.Post(
        content,
        id=file_id,
        source_url=url,
        classification=classification,
        ingested_at=datetime.now().isoformat(),
        checksum=checksum
    )

    # Write to destination
    with open(dest_path, 'w', encoding='utf-8') as f:
        f.write(frontmatter.dumps(doc))

    # Record in database
    conn = get_connection()
    try:
        conn.execute("BEGIN")

        conn.execute("""
            INSERT INTO raw_files (id, filename, path, classification, checksum, source_url)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            file_id,
            dest_filename,
            str(dest_path.relative_to(root)),
            classification,
            checksum,
            url
        ))

        conn.execute("COMMIT")

        print(f"✓ Ingested: {dest_filename}")
        print(f"  ID: {file_id}")
        print(f"  Path: {dest_path.relative_to(root)}")

    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"✗ Database error: {e}")
        # Clean up file
        if dest_path.exists():
            dest_path.unlink()
        raise


def cmd_ingest_pdf(
    filepath: str,
    classification: str,
    use_marker: bool = True,
    max_pages: int = None
):
    """
    Extract PDF to markdown and ingest.

    Uses marker-pdf for high-quality extraction (OCR, tables, formatting)
    with fallback to basic pypdf if marker not available.

    Args:
        filepath: Path to PDF file
        classification: public|internal|confidential
        use_marker: Use marker-pdf if available (default: True)
        max_pages: Maximum pages to extract (None = all)
    """
    from kb.pdf_extract import extract_pdf, is_marker_available

    filepath = Path(filepath).expanduser().resolve()

    if not filepath.exists():
        print(f"✗ File not found: {filepath}")
        return

    print(f"Extracting PDF: {filepath.name}")
    print(f"Classification: {classification}")

    # Show extraction method
    if use_marker and is_marker_available():
        print("Method: marker-pdf (high quality)")
    elif use_marker:
        print("Method: pypdf (basic) - marker-pdf not installed")
    else:
        print("Method: pypdf (basic)")

    # Extract PDF
    try:
        result = extract_pdf(
            filepath,
            use_marker=use_marker,
            fallback_to_basic=True,
            max_pages=max_pages
        )

        content = result['content']
        pages = result['pages']
        method = result['method']
        pdf_metadata = result['metadata']
        images = result.get('images', [])

    except Exception as e:
        print(f"✗ Error extracting PDF: {e}")
        logger.error(f"PDF extraction failed for {filepath}: {e}", exc_info=True)
        return

    # Calculate checksum
    checksum = hashlib.sha256(content.encode('utf-8')).hexdigest()

    # Generate unique ID
    file_id = str(uuid.uuid4())

    # Store in raw/{classification}/
    root = get_project_root()
    dest_dir = root / 'raw' / classification
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_filename = f"{file_id}-{filepath.stem}.md"
    dest_path = dest_dir / dest_filename

    # Build frontmatter metadata
    fm_metadata = {
        'id': file_id,
        'source_file': str(filepath),
        'source_type': 'pdf',
        'extraction_method': method,
        'classification': classification,
        'ingested_at': datetime.now().isoformat(),
        'checksum': checksum,
        'pages': pages
    }

    # Add PDF metadata if available
    if pdf_metadata:
        fm_metadata['pdf_metadata'] = pdf_metadata

    # Add image info if available
    if images:
        fm_metadata['images_extracted'] = len(images)

    # Create document with frontmatter
    doc = frontmatter.Post(content, **fm_metadata)

    # Write to destination
    with open(dest_path, 'w', encoding='utf-8') as f:
        f.write(frontmatter.dumps(doc))

    # Record in database
    conn = get_connection()
    try:
        conn.execute("BEGIN")

        conn.execute("""
            INSERT INTO raw_files (id, filename, path, classification, checksum, source_url)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            file_id,
            dest_filename,
            str(dest_path.relative_to(root)),
            classification,
            checksum,
            str(filepath)
        ))

        conn.execute("COMMIT")

        print(f"✓ Extracted and ingested: {dest_filename}")
        print(f"  ID: {file_id}")
        print(f"  Path: {dest_path.relative_to(root)}")
        print(f"  Pages: {pages}")
        print(f"  Method: {method}")
        if images:
            print(f"  Images: {len(images)}")

    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"✗ Database error: {e}")
        # Clean up file
        if dest_path.exists():
            dest_path.unlink()
        raise


def cmd_ingest_repo(repo_url: str, classification: str):
    """
    Clone and index git repository.

    Phase 1: Clone repo, extract README only.
    Phase 2+: Full indexing with code structure.

    Args:
        repo_url: GitHub URL
        classification: public|internal|confidential (default: internal for repos)
    """
    print(f"Cloning repository: {repo_url}")
    print(f"Classification: {classification}")

    try:
        from git import Repo
        import tempfile

        # Generate unique ID
        file_id = str(uuid.uuid4())

        # Clone to temporary directory
        with tempfile.TemporaryDirectory() as tmpdir:
            print("Cloning... (this may take a moment)")

            repo = Repo.clone_from(
                repo_url,
                tmpdir,
                depth=1  # Shallow clone
            )

            # Extract repository name
            repo_name = repo_url.rstrip('/').split('/')[-1].replace('.git', '')

            # Find README
            readme_content = ""
            readme_candidates = ['README.md', 'README.txt', 'README', 'readme.md']

            for readme_name in readme_candidates:
                readme_path = Path(tmpdir) / readme_name
                if readme_path.exists():
                    with open(readme_path, 'r', encoding='utf-8') as f:
                        readme_content = f.read()
                    break

            if not readme_content:
                readme_content = f"# {repo_name}\n\nNo README found."

            # Create summary
            content = f"""# Repository: {repo_name}

**Source:** {repo_url}

## README

{readme_content}

## Repository Info

- Cloned at: {datetime.now().isoformat()}
- Shallow clone (depth: 1)
"""

    except Exception as e:
        print(f"✗ Error cloning repository: {e}")
        return

    # Calculate checksum
    checksum = hashlib.sha256(content.encode('utf-8')).hexdigest()

    # Store in raw/{classification}/
    root = get_project_root()
    dest_dir = root / 'raw' / classification
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_filename = f"{file_id}-{repo_name}-repo.md"
    dest_path = dest_dir / dest_filename

    # Create document with frontmatter
    doc = frontmatter.Post(
        content,
        id=file_id,
        source_url=repo_url,
        source_type='git_repo',
        repo_name=repo_name,
        classification=classification,
        ingested_at=datetime.now().isoformat(),
        checksum=checksum
    )

    # Write to destination
    with open(dest_path, 'w', encoding='utf-8') as f:
        f.write(frontmatter.dumps(doc))

    # Record in database
    conn = get_connection()
    try:
        conn.execute("BEGIN")

        conn.execute("""
            INSERT INTO raw_files (id, filename, path, classification, checksum, source_url)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            file_id,
            dest_filename,
            str(dest_path.relative_to(root)),
            classification,
            checksum,
            repo_url
        ))

        conn.execute("COMMIT")

        print(f"✓ Repository ingested: {dest_filename}")
        print(f"  ID: {file_id}")
        print(f"  Path: {dest_path.relative_to(root)}")

    except Exception as e:
        conn.execute("ROLLBACK")
        print(f"✗ Database error: {e}")
        # Clean up file
        if dest_path.exists():
            dest_path.unlink()
        raise
