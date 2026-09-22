# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
from .base import EventMetricDataSource


class ModelRetryEventDataSource(EventMetricDataSource):
    @classmethod
    def record(cls, event: str, **fields) -> None:
        cls._emit(f"model_retry.{event}", fields)
