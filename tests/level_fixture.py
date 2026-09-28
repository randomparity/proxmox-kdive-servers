"""Fixture source injected by explicit tests; never loaded by production entrypoints."""

from pathlib import Path


# Executed in guest_verify's namespace so transported checks use the same fixture.
def prepare_contract_test(request):
    path = Path("/var/lib/kdive-contract-test")
    with path.open("x") as stream:
        stream.write("snapshot-contract-v1\n")
    return {"marker": "snapshot-contract-v1"}


def check_contract_test(request, content):
    if content != {"marker": "snapshot-contract-v1"} or (
        Path("/var/lib/kdive-contract-test").read_text() != "snapshot-contract-v1\n"
    ):
        raise ValueError("Fixture marker or content differs")


LEVELS = (
    {
        "name": "contract-test",
        "parent": "clean",
        "prepare": prepare_contract_test,
        "check": check_contract_test,
    },
)
