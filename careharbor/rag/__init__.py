"""Vectorless retrieval: a document tree + BM25 ranking (+ optional LLM navigation).

No embeddings and no vector database. Every Markdown document becomes a tree
(document -> sections). Sections are ranked with BM25 (classic lexical
relevance scoring) using field boosts and query expansion for synonyms,
Hinglish and UK/US spellings. In "tree" mode an LLM then reads a compact table
of contents of the shortlisted documents and picks the sections that answer
the question (reasoning-based navigation, like a human using a book's index).
"""

from careharbor.rag.index import RagIndex, SearchHit, get_index

__all__ = ["RagIndex", "SearchHit", "get_index"]
