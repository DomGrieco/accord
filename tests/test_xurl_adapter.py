from __future__ import annotations

import pytest

from human_gate.xurl_adapter import XurlPublisher


def test_xurl_publisher_stays_disabled_until_live_adapter_is_implemented() -> None:
    publisher = XurlPublisher()

    with pytest.raises(NotImplementedError):
        publisher.publish({"text": "fixture only"})