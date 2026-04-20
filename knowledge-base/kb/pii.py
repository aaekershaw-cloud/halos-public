"""PII (Personally Identifiable Information) detection and redaction"""

import re
import logging
import hashlib
from typing import Dict, List, Set, Tuple, Optional
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class PIIType(Enum):
    """Types of PII that can be detected"""
    EMAIL = "email"
    PHONE = "phone"
    SSN = "ssn"
    CREDIT_CARD = "credit_card"
    API_KEY = "api_key"
    AWS_KEY = "aws_key"
    GITHUB_TOKEN = "github_token"
    PRIVATE_KEY = "private_key"
    PASSWORD = "password"
    IP_ADDRESS = "ip_address"
    CUSTOM = "custom"


class RedactionStrategy(Enum):
    """Strategies for redacting PII"""
    MASK = "mask"  # Replace with ***
    HASH = "hash"  # Replace with SHA256 hash
    REMOVE = "remove"  # Remove entirely
    PLACEHOLDER = "placeholder"  # Replace with [REDACTED:TYPE]


@dataclass
class PIIMatch:
    """Represents a detected PII match"""
    pii_type: PIIType
    value: str
    start: int
    end: int
    confidence: float  # 0.0 to 1.0
    context: str  # Surrounding text for verification


# PII Detection Patterns
PII_PATTERNS = {
    PIIType.EMAIL: [
        r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
    ],
    PIIType.PHONE: [
        r'\b(?:\+?1[-.\s]?)?\(?([0-9]{3})\)?[-.\s]?([0-9]{3})[-.\s]?([0-9]{4})\b',
        r'\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b'
    ],
    PIIType.SSN: [
        r'\b\d{3}-\d{2}-\d{4}\b',
        r'\b\d{9}\b'
    ],
    PIIType.CREDIT_CARD: [
        r'\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13}|3(?:0[0-5]|[68][0-9])[0-9]{11}|6(?:011|5[0-9]{2})[0-9]{12}|(?:2131|1800|35\d{3})\d{11})\b'
    ],
    PIIType.API_KEY: [
        r'(?i)(api[_-]?key|apikey)\s*[:=]\s*["\']?([a-zA-Z0-9_\-]{20,})["\']?',
        r'(?i)(access[_-]?token|token)\s*[:=]\s*["\']?([a-zA-Z0-9_\-]{20,})["\']?'
    ],
    PIIType.AWS_KEY: [
        r'(?i)AKIA[0-9A-Z]{16}',
        r'(?i)aws[_-]?(secret|access)[_-]?key["\']?\s*[:=]\s*["\']?([a-zA-Z0-9/+=]{40})["\']?'
    ],
    PIIType.GITHUB_TOKEN: [
        r'(?i)gh[pousr]_[a-zA-Z0-9]{36,}',
        r'(?i)github[_-]?token\s*[:=]\s*["\']?([a-zA-Z0-9_\-]{20,})["\']?'
    ],
    PIIType.PRIVATE_KEY: [
        r'-----BEGIN (?:RSA |DSA |EC )?PRIVATE KEY-----',
        r'(?i)private[_-]?key\s*[:=]\s*["\']?([a-zA-Z0-9/+=\n]{40,})["\']?'
    ],
    PIIType.PASSWORD: [
        r'(?i)password\s*[:=]\s*["\']?([^"\'\s]{8,})["\']?',
        r'(?i)passwd\s*[:=]\s*["\']?([^"\'\s]{8,})["\']?'
    ],
    PIIType.IP_ADDRESS: [
        r'\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b'
    ]
}


def detect_pii(text: str, pii_types: Optional[List[PIIType]] = None) -> List[PIIMatch]:
    """
    Detect PII in text using pattern matching.

    Args:
        text: Text to scan
        pii_types: Specific PII types to detect (default: all)

    Returns:
        List of PII matches found
    """
    if pii_types is None:
        pii_types = list(PIIType)

    matches = []

    for pii_type in pii_types:
        if pii_type not in PII_PATTERNS:
            continue

        patterns = PII_PATTERNS[pii_type]

        for pattern in patterns:
            for match in re.finditer(pattern, text):
                # Extract matched value
                value = match.group(0)
                start = match.start()
                end = match.end()

                # Get context (50 chars before and after)
                context_start = max(0, start - 50)
                context_end = min(len(text), end + 50)
                context = text[context_start:context_end]

                # Calculate confidence based on pattern strength
                confidence = calculate_confidence(pii_type, value)

                matches.append(PIIMatch(
                    pii_type=pii_type,
                    value=value,
                    start=start,
                    end=end,
                    confidence=confidence,
                    context=context
                ))

    # Sort by position
    matches.sort(key=lambda m: m.start)

    # Remove duplicates (same location, different patterns)
    unique_matches = []
    seen_positions = set()

    for match in matches:
        pos = (match.start, match.end)
        if pos not in seen_positions:
            unique_matches.append(match)
            seen_positions.add(pos)

    return unique_matches


def calculate_confidence(pii_type: PIIType, value: str) -> float:
    """
    Calculate confidence score for a PII match.

    Args:
        pii_type: Type of PII
        value: Matched value

    Returns:
        Confidence score 0.0-1.0
    """
    # Base confidence by type
    base_confidence = {
        PIIType.EMAIL: 0.9,
        PIIType.PHONE: 0.7,
        PIIType.SSN: 0.8,
        PIIType.CREDIT_CARD: 0.85,
        PIIType.API_KEY: 0.75,
        PIIType.AWS_KEY: 0.95,
        PIIType.GITHUB_TOKEN: 0.95,
        PIIType.PRIVATE_KEY: 0.99,
        PIIType.PASSWORD: 0.6,
        PIIType.IP_ADDRESS: 0.5
    }.get(pii_type, 0.5)

    # Adjust based on value characteristics
    if pii_type == PIIType.CREDIT_CARD:
        # Validate with Luhn algorithm
        if luhn_check(value):
            return min(1.0, base_confidence + 0.1)

    if pii_type == PIIType.SSN:
        # SSN with dashes is more confident
        if '-' in value:
            return min(1.0, base_confidence + 0.1)

    return base_confidence


def luhn_check(card_number: str) -> bool:
    """
    Validate credit card number using Luhn algorithm.

    Args:
        card_number: Card number string

    Returns:
        True if valid
    """
    digits = [int(d) for d in card_number if d.isdigit()]

    if len(digits) < 13:
        return False

    # Luhn algorithm
    checksum = 0
    for i, digit in enumerate(reversed(digits)):
        if i % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit

    return checksum % 10 == 0


def redact_pii(
    text: str,
    matches: List[PIIMatch],
    strategy: RedactionStrategy = RedactionStrategy.PLACEHOLDER,
    confidence_threshold: float = 0.7
) -> str:
    """
    Redact PII from text.

    Args:
        text: Original text
        matches: PII matches to redact
        strategy: Redaction strategy to use
        confidence_threshold: Only redact matches above this confidence

    Returns:
        Redacted text
    """
    # Filter by confidence
    matches_to_redact = [m for m in matches if m.confidence >= confidence_threshold]

    # Sort by position (reverse order to maintain indices)
    matches_to_redact.sort(key=lambda m: m.start, reverse=True)

    redacted_text = text

    for match in matches_to_redact:
        start = match.start
        end = match.end
        original_value = match.value

        # Apply redaction strategy
        if strategy == RedactionStrategy.MASK:
            replacement = '*' * len(original_value)

        elif strategy == RedactionStrategy.HASH:
            hash_value = hashlib.sha256(original_value.encode()).hexdigest()[:16]
            replacement = f"[HASH:{hash_value}]"

        elif strategy == RedactionStrategy.REMOVE:
            replacement = ""

        elif strategy == RedactionStrategy.PLACEHOLDER:
            replacement = f"[REDACTED:{match.pii_type.value.upper()}]"

        else:
            replacement = "[REDACTED]"

        # Replace in text
        redacted_text = redacted_text[:start] + replacement + redacted_text[end:]

    return redacted_text


def scan_file(file_path: str, pii_types: Optional[List[PIIType]] = None) -> Dict:
    """
    Scan a file for PII.

    Args:
        file_path: Path to file
        pii_types: Specific PII types to detect

    Returns:
        {
            'file_path': str,
            'has_pii': bool,
            'matches': List[PIIMatch],
            'by_type': Dict[PIIType, int]
        }
    """
    try:
        with open(file_path, 'r') as f:
            text = f.read()
    except Exception as e:
        logger.error(f"Failed to read file {file_path}: {e}")
        return {
            'file_path': file_path,
            'has_pii': False,
            'matches': [],
            'by_type': {},
            'error': str(e)
        }

    matches = detect_pii(text, pii_types)

    # Count by type
    by_type = {}
    for match in matches:
        by_type[match.pii_type] = by_type.get(match.pii_type, 0) + 1

    return {
        'file_path': file_path,
        'has_pii': len(matches) > 0,
        'matches': matches,
        'by_type': by_type
    }


def scan_article(article_id: str) -> Dict:
    """
    Scan a compiled article for PII.

    Args:
        article_id: Article ID

    Returns:
        Scan results dictionary
    """
    from kb.db import get_connection

    conn = get_connection()

    cursor = conn.execute("""
        SELECT title, content_path FROM articles WHERE id = ?
    """, (article_id,))

    row = cursor.fetchone()
    if not row:
        return {
            'article_id': article_id,
            'has_pii': False,
            'error': 'Article not found'
        }

    result = scan_file(row['content_path'])
    result['article_id'] = article_id
    result['title'] = row['title']

    return result


def scan_all_articles(confidence_threshold: float = 0.7) -> List[Dict]:
    """
    Scan all articles for PII.

    Args:
        confidence_threshold: Minimum confidence to report

    Returns:
        List of articles with PII detected
    """
    from kb.db import get_connection

    conn = get_connection()

    cursor = conn.execute("SELECT id FROM articles")
    article_ids = [row['id'] for row in cursor]

    logger.info(f"Scanning {len(article_ids)} articles for PII...")

    articles_with_pii = []

    for article_id in article_ids:
        result = scan_article(article_id)

        if result.get('has_pii'):
            # Filter by confidence
            high_conf_matches = [
                m for m in result['matches']
                if m.confidence >= confidence_threshold
            ]

            if high_conf_matches:
                result['matches'] = high_conf_matches
                articles_with_pii.append(result)

    logger.info(f"Found PII in {len(articles_with_pii)} articles")

    return articles_with_pii


def get_pii_summary(matches: List[PIIMatch]) -> Dict:
    """
    Get summary statistics from PII matches.

    Args:
        matches: List of PII matches

    Returns:
        Summary dictionary
    """
    by_type = {}
    by_confidence = {'high': 0, 'medium': 0, 'low': 0}

    for match in matches:
        # Count by type
        by_type[match.pii_type.value] = by_type.get(match.pii_type.value, 0) + 1

        # Count by confidence
        if match.confidence >= 0.8:
            by_confidence['high'] += 1
        elif match.confidence >= 0.6:
            by_confidence['medium'] += 1
        else:
            by_confidence['low'] += 1

    return {
        'total': len(matches),
        'by_type': by_type,
        'by_confidence': by_confidence,
        'unique_types': len(by_type)
    }
