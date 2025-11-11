#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""SIP Transport for Pipecat.

This module provides SIP (Session Initiation Protocol) transport support for
Pipecat, enabling voice bots to accept incoming telephone calls via SIP/RTP.
"""

from .transport import SIPParams, SIPTransport

__all__ = ["SIPParams", "SIPTransport"]
