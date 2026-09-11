"""High-availability coordination primitives for Takealot Ops."""

from takealot_ops.ha.witness import (
    WitnessBusyError,
    WitnessLease,
    WitnessLeaseConfig,
    WitnessLeaseLostError,
    WitnessMessage,
    WitnessProtocolError,
    acquire_witness_lease,
    build_ssh_command,
    parse_witness_message,
)

__all__ = [
    "WitnessBusyError",
    "WitnessLease",
    "WitnessLeaseConfig",
    "WitnessLeaseLostError",
    "WitnessMessage",
    "WitnessProtocolError",
    "acquire_witness_lease",
    "build_ssh_command",
    "parse_witness_message",
]
