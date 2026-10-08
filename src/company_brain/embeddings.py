"""Real local embeddings. Test doubles live exclusively under tests/."""

import os
import threading

DIMENSIONS = 384


class LocalEmbedder:
    def __init__(self, model, revision):
        self.model_name = model
        self.revision = revision
        self._model = None
        self._lock = threading.Lock()

    @property
    def model_id(self):
        return f"{self.model_name}@{self.revision}"

    def _load(self):
        with self._lock:
            if self._model is None:
                os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
                from sentence_transformers import SentenceTransformer
                import torch

                torch.set_num_threads(int(os.environ.get("BRAIN_EMBEDDING_THREADS", "2")))
                self._model = SentenceTransformer(
                    self.model_name,
                    revision=self.revision,
                    device="cpu",
                    trust_remote_code=False,
                )
            return self._model

    def chunks(self, text):
        """Tokenizer-bounded chunks preserve all source text; max 126 model content tokens."""
        model = self._load()
        tokenizer = model.tokenizer
        budget = model.max_seq_length - tokenizer.num_special_tokens_to_add(pair=False)
        offset = 0
        while offset < len(text):
            # A short window bounds tokenization memory, including pathological long words.
            end = min(len(text), offset + 2000)
            tokens = tokenizer(
                text[offset:end], add_special_tokens=False, return_offsets_mapping=True
            )
            if len(tokens["input_ids"]) > budget:
                end = offset + tokens["offset_mapping"][budget - 1][1]
            end = max(end, offset + 1)
            yield text[offset:end]
            offset = end

    def encode(self, texts):
        if not texts:
            return []
        model = self._load()
        with self._lock:
            return model.encode(
                texts, batch_size=16, normalize_embeddings=True, show_progress_bar=False
            ).tolist()
