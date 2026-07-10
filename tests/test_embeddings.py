"""Tests for equipa.embeddings module.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from equipa.embeddings import (
    cosine_similarity,
    embed_and_store_episode,
    embed_and_store_lesson,
    find_similar_by_embedding,
    get_embedding,
)


class TestCosineSimilarity:
    """Test cosine_similarity pure Python implementation."""

    def test_identical_vectors(self):
        """Identical vectors have similarity 1.0."""
        a = [1.0, 2.0, 3.0]
        assert cosine_similarity(a, a) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        """Orthogonal vectors have similarity 0.0."""
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        assert cosine_similarity(a, b) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        """Opposite vectors have similarity -1.0."""
        a = [1.0, 0.0, 0.0]
        b = [-1.0, 0.0, 0.0]
        assert cosine_similarity(a, b) == pytest.approx(-1.0)

    def test_mismatched_dimensions_returns_zero(self):
        """Mismatched vector dimensions return 0.0."""
        a = [1.0, 2.0]
        b = [1.0, 2.0, 3.0]
        assert cosine_similarity(a, b) == 0.0

    def test_empty_vectors_return_zero(self):
        """Empty vectors return 0.0."""
        assert cosine_similarity([], []) == 0.0
        assert cosine_similarity([1.0], []) == 0.0

    def test_zero_vector_returns_zero(self):
        """Zero-magnitude vectors return 0.0."""
        a = [0.0, 0.0, 0.0]
        b = [1.0, 2.0, 3.0]
        assert cosine_similarity(a, b) == 0.0


class TestGetEmbedding:
    """Test get_embedding Ollama API client."""

    @patch("urllib.request.urlopen")
    def test_successful_embedding_request(self, mock_urlopen):
        """Successful API call returns embedding vector."""
        mock_response = Mock()
        mock_response.read.return_value = json.dumps({
            "embedding": [0.1, 0.2, 0.3]
        }).encode("utf-8")
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = get_embedding("test text", model="test-model", base_url="http://test:1234")

        assert result == [0.1, 0.2, 0.3]
        assert mock_urlopen.call_count == 1
        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        assert request.full_url == "http://test:1234/api/embeddings"
        payload = json.loads(request.data.decode("utf-8"))
        assert payload == {"model": "test-model", "prompt": "test text"}

    @patch("urllib.request.urlopen")
    def test_empty_text_returns_none(self, mock_urlopen):
        """Empty text returns None without calling API."""
        result = get_embedding("   ", model="test-model")
        assert result is None
        assert mock_urlopen.call_count == 0

    @patch("urllib.request.urlopen")
    def test_ollama_down_returns_none(self, mock_urlopen):
        """Connection error returns None gracefully."""
        # Use realistic transport error — narrowed except catches URLError/OSError
        mock_urlopen.side_effect = ConnectionRefusedError("Connection refused")
        result = get_embedding("test text")
        assert result is None

    @patch("urllib.request.urlopen")
    def test_timeout_returns_none(self, mock_urlopen):
        """Timeout returns None gracefully."""
        import urllib.error
        mock_urlopen.side_effect = urllib.error.URLError("timeout")
        result = get_embedding("test text")
        assert result is None

    @patch("urllib.request.urlopen")
    def test_default_parameters(self, mock_urlopen):
        """Default model and base_url are used."""
        mock_response = Mock()
        mock_response.read.return_value = json.dumps({"embedding": [0.1]}).encode("utf-8")
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=False)
        mock_urlopen.return_value = mock_response

        get_embedding("test")

        request = mock_urlopen.call_args[0][0]
        assert request.full_url == "http://localhost:11434/api/embeddings"
        payload = json.loads(request.data.decode("utf-8"))
        assert payload["model"] == "all-MiniLM-L6-v2"


class TestEmbedAndStoreLesson:
    """Test embed_and_store_lesson database integration."""

    @patch("equipa.embeddings.get_embedding")
    @patch("equipa.embeddings.sqlite3.connect")
    def test_successful_lesson_storage(self, mock_connect, mock_get_embedding):
        """Successful embedding is stored in lessons table."""
        mock_get_embedding.return_value = [0.1, 0.2, 0.3]
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn

        result = embed_and_store_lesson(123, "lesson content")

        assert result is True
        mock_get_embedding.assert_called_once_with(
            "lesson content",
            model="all-MiniLM-L6-v2",
            base_url="http://localhost:11434",
        )
        # db.py issues PRAGMA busy_timeout on connect (task 2700), so execute()
        # is called more than once; assert the INSERT call specifically.
        mock_conn.execute.assert_any_call(
            "UPDATE lessons_learned SET embedding = ? WHERE id = ?",
            (json.dumps([0.1, 0.2, 0.3]), 123),
        )
        mock_conn.commit.assert_called_once()
        mock_conn.close.assert_called_once()

    @patch("equipa.embeddings.get_embedding")
    def test_ollama_failure_returns_false(self, mock_get_embedding):
        """Ollama failure returns False without DB write."""
        mock_get_embedding.return_value = None
        result = embed_and_store_lesson(123, "test")
        assert result is False

    @patch("equipa.embeddings.get_embedding")
    @patch("equipa.embeddings.sqlite3.connect")
    def test_db_error_returns_false(self, mock_connect, mock_get_embedding):
        """Database error returns False."""
        mock_get_embedding.return_value = [0.1, 0.2]
        # Use sqlite3.Error subclass — narrowed except catches sqlite3.Error
        mock_connect.side_effect = sqlite3.OperationalError("DB error")
        result = embed_and_store_lesson(123, "test")
        assert result is False

    @patch("equipa.embeddings.get_embedding")
    @patch("equipa.embeddings.sqlite3.connect")
    def test_custom_config(self, mock_connect, mock_get_embedding):
        """Custom dispatch_config overrides model and base_url."""
        mock_get_embedding.return_value = [0.1]
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn

        config = {
            "ollama_model": "custom-model",
            "ollama_base_url": "http://custom:8080",
        }
        embed_and_store_lesson(123, "test", dispatch_config=config)

        mock_get_embedding.assert_called_once_with(
            "test",
            model="custom-model",
            base_url="http://custom:8080",
        )


class TestEmbedAndStoreEpisode:
    """Test embed_and_store_episode database integration."""

    @patch("equipa.embeddings.get_embedding")
    @patch("equipa.embeddings.sqlite3.connect")
    def test_successful_episode_storage(self, mock_connect, mock_get_embedding):
        """Successful embedding is stored in episodes table."""
        mock_get_embedding.return_value = [0.4, 0.5, 0.6]
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn

        result = embed_and_store_episode(456, "episode content")

        assert result is True
        # db.py issues PRAGMA busy_timeout on connect (task 2700), so execute()
        # is called more than once; assert the INSERT call specifically.
        mock_conn.execute.assert_any_call(
            "UPDATE agent_episodes SET embedding = ? WHERE id = ?",
            (json.dumps([0.4, 0.5, 0.6]), 456),
        )
        mock_conn.commit.assert_called_once()
        mock_conn.close.assert_called_once()

    @patch("equipa.embeddings.get_embedding")
    def test_ollama_failure_returns_false(self, mock_get_embedding):
        """Ollama failure returns False without DB write."""
        mock_get_embedding.return_value = None
        result = embed_and_store_episode(456, "test")
        assert result is False


class TestFindSimilarByEmbedding:
    """Test find_similar_by_embedding brute-force search."""

    @patch("equipa.embeddings.get_embedding")
    @patch("equipa.embeddings.sqlite3.connect")
    def test_successful_similarity_search(self, mock_connect, mock_get_embedding):
        """Returns top_k most similar rows sorted by descending score."""
        query_emb = [1.0, 0.0, 0.0]
        mock_get_embedding.return_value = query_emb

        mock_cursor = Mock()
        mock_cursor.fetchall.return_value = [
            (1, json.dumps([1.0, 0.0, 0.0])),  # similarity 1.0
            (2, json.dumps([0.5, 0.5, 0.0])),  # similarity ~0.707
            (3, json.dumps([0.0, 1.0, 0.0])),  # similarity 0.0
            (4, json.dumps([-1.0, 0.0, 0.0])), # similarity -1.0
        ]
        mock_conn = MagicMock()
        mock_conn.execute.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        result = find_similar_by_embedding("query", "lessons", top_k=3)

        assert len(result) == 3
        assert result[0][0] == 1  # ID 1 has highest similarity
        assert result[0][1] == pytest.approx(1.0)
        assert result[1][0] == 2  # ID 2 second
        assert result[2][0] == 3  # ID 3 third

    @patch("equipa.embeddings.get_embedding")
    def test_invalid_table_returns_empty(self, mock_get_embedding):
        """Invalid table name returns empty list."""
        result = find_similar_by_embedding("query", "invalid_table")
        assert result == []
        mock_get_embedding.assert_not_called()

    @patch("equipa.embeddings.get_embedding")
    def test_ollama_failure_returns_empty(self, mock_get_embedding):
        """Ollama failure returns empty list."""
        mock_get_embedding.return_value = None
        result = find_similar_by_embedding("query", "lessons")
        assert result == []

    @patch("equipa.embeddings.get_embedding")
    @patch("equipa.embeddings.sqlite3.connect")
    def test_db_error_returns_empty(self, mock_connect, mock_get_embedding):
        """Database error returns empty list."""
        mock_get_embedding.return_value = [0.1, 0.2]
        # Use sqlite3.Error subclass — narrowed except catches sqlite3.Error
        mock_connect.side_effect = sqlite3.OperationalError("DB error")
        result = find_similar_by_embedding("query", "lessons")
        assert result == []

    @patch("equipa.embeddings.get_embedding")
    @patch("equipa.embeddings.sqlite3.connect")
    def test_malformed_json_skipped(self, mock_connect, mock_get_embedding):
        """Rows with malformed JSON embeddings are skipped."""
        mock_get_embedding.return_value = [1.0, 0.0]
        mock_cursor = Mock()
        mock_cursor.fetchall.return_value = [
            (1, "not valid json"),
            (2, json.dumps([1.0, 0.0])),  # valid
        ]
        mock_conn = MagicMock()
        mock_conn.execute.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        result = find_similar_by_embedding("query", "episodes", top_k=5)

        assert len(result) == 1
        assert result[0][0] == 2

    @patch("equipa.embeddings.get_embedding")
    @patch("equipa.embeddings.sqlite3.connect")
    def test_top_k_limit_respected(self, mock_connect, mock_get_embedding):
        """top_k limit is respected."""
        mock_get_embedding.return_value = [1.0]
        mock_cursor = Mock()
        mock_cursor.fetchall.return_value = [
            (i, json.dumps([1.0])) for i in range(10)
        ]
        mock_conn = MagicMock()
        mock_conn.execute.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        result = find_similar_by_embedding("query", "lessons", top_k=3)

        assert len(result) == 3
