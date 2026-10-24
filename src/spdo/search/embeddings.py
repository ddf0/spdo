"""Векторные представления текстов моделью sentence-transformers.

Модель загружается один раз на процесс и только локально: обращений к
внешним API нет (``CLAUDE.md``, ТЗ). Векторы нормируются, поэтому
косинусное сходство равно скалярному произведению.
"""

import threading
import time

import numpy as np

from spdo.search.base import SearchUnavailableError

#: Пауза перед повторной попыткой загрузить модель после неудачи, секунды.
RETRY_AFTER = 60.0


class Encoder:
    """Кодировщик текстов в векторы фиксированной размерности.

    Модель берётся только из локального кеша; скачивание выполняется явно
    (``spdo download-model``) при установке. После неудачной загрузки
    повторная попытка делается не раньше чем через :data:`RETRY_AFTER`, чтобы
    недоступная модель не задерживала каждый запрос.

    Attributes:
        model_name: Идентификатор модели sentence-transformers.
        dim: Ожидаемая размерность вектора.
        device: Устройство вычислений (``cpu``, ``cuda``).
        max_tokens: Наибольшая длина текста в токенах.
        threads: Число потоков torch на CPU; 0 — по умолчанию torch.
        allow_download: Разрешено ли скачивание модели, если её нет в кеше.
    """

    def __init__(
        self,
        model_name: str,
        dim: int,
        device: str = "cpu",
        max_tokens: int = 512,
        threads: int = 0,
        allow_download: bool = False,
    ) -> None:
        """Создаёт кодировщик; модель загружается при первом использовании.

        Args:
            model_name: Идентификатор модели.
            dim: Ожидаемая размерность вектора.
            device: Устройство вычислений.
            max_tokens: Наибольшая длина текста в токенах; длиннее —
                обрезается. Ограничивает память и время на длинных описаниях.
            threads: Число потоков torch на CPU; 0 — не менять.
            allow_download: Разрешить скачивание модели с Hugging Face.
        """
        self.model_name = model_name
        self.dim = dim
        self.device = device
        self.max_tokens = max_tokens
        self.threads = threads
        self.allow_download = allow_download
        self._model = None
        self._failed_at: float | None = None
        self._lock = threading.Lock()

    def load(self) -> None:
        """Загружает модель и проверяет размерность её векторов.

        Raises:
            SearchUnavailableError: Модель не загрузилась (в том числе
                недавно — повтор отложен) или её размерность не совпадает с
                размерностью столбца поискового индекса.
        """
        with self._lock:
            if self._model is not None:
                return
            if self._failed_at is not None and time.monotonic() - self._failed_at < RETRY_AFTER:
                raise SearchUnavailableError(f"Модель {self.model_name} недоступна")
            try:
                self._model = self._load_model()
            except SearchUnavailableError:
                self._failed_at = time.monotonic()
                raise

    def _load_model(self):  # noqa: ANN202 — тип модели из необязательной зависимости
        """Загружает модель из кеша (или скачивает, если разрешено)."""
        try:
            import torch
            from sentence_transformers import SentenceTransformer

            if self.threads:
                torch.set_num_threads(self.threads)
            model = SentenceTransformer(
                self.model_name, device=self.device, local_files_only=not self.allow_download
            )
        except Exception as exc:  # noqa: BLE001 — любая ошибка загрузки = поиск недоступен
            raise SearchUnavailableError(f"Модель {self.model_name} не загружена: {exc}") from exc
        model.max_seq_length = min(model.max_seq_length, self.max_tokens)
        actual = model.get_embedding_dimension()
        if actual != self.dim:
            raise SearchUnavailableError(
                f"Размерность модели {actual} не совпадает с SPDO_EMBEDDING_DIM={self.dim}"
            )
        return model

    def encode(self, texts: list[str], batch_size: int = 64) -> np.ndarray:
        """Вычисляет нормированные векторы текстов.

        Args:
            texts: Тексты.
            batch_size: Размер пакета при вычислении.

        Returns:
            Матрица ``len(texts) × dim`` типа ``float32``.

        Raises:
            SearchUnavailableError: Модель недоступна или вычисление не удалось.
        """
        self.load()
        model = self._model
        if model is None:
            raise SearchUnavailableError(f"Модель {self.model_name} не загружена")
        try:
            vectors = model.encode(
                texts, batch_size=batch_size, normalize_embeddings=True, show_progress_bar=False
            )
        except Exception as exc:  # noqa: BLE001 — нехватка памяти и др. = поиск недоступен
            raise SearchUnavailableError(f"Не удалось вычислить векторы: {exc}") from exc
        return np.asarray(vectors, dtype=np.float32)
