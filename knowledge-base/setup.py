#!/usr/bin/env python3
"""Knowledge Base System - Setup"""

from setuptools import setup, find_packages

with open('README.md', 'r', encoding='utf-8') as f:
    long_description = f.read()

setup(
    name='knowledge-base',
    version='1.0.0',
    description='LLM-powered knowledge base with human-supervised compilation',
    long_description=long_description,
    long_description_content_type='text/markdown',
    author='HalOS Contributors',
    python_requires='>=3.9',
    packages=find_packages(),
    include_package_data=True,
    install_requires=[
        'click>=8.0',
        'pypdf>=3.0.0',
        'gitpython>=3.1.0',
        'anthropic>=0.21.0',
        'python-frontmatter>=1.0.0',
        'requests>=2.31.0',
        'pyyaml>=6.0',
    ],
    extras_require={
        'pii': [
            'presidio-analyzer>=2.2.0',
            'presidio-anonymizer>=2.2.0',
        ],
        'full': [
            'presidio-analyzer>=2.2.0',
            'presidio-anonymizer>=2.2.0',
            'marker-pdf>=0.2.0',
        ],
        'dev': [
            'pytest>=7.0',
            'pytest-cov>=4.0',
            'black>=23.0',
            'mypy>=1.0',
        ]
    },
    entry_points={
        'console_scripts': [
            'kb=kb.cli:main',
        ],
    },
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Intended Audience :: Developers',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
    ],
)
