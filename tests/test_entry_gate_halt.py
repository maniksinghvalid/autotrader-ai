from autotrader.lifecycle import EntryGate


def test_defaults_not_halted():
    g = EntryGate()
    assert g.halted is False


def test_halt_sets_flag_and_closes_entries():
    g = EntryGate(enabled=True)
    g.halt()
    assert g.halted is True
    assert g.entries_enabled is False  # halting also closes entries


def test_open_does_not_reenable_after_halt():
    g = EntryGate(enabled=False)
    g.halt()
    g.open()
    assert g.entries_enabled is False  # a halted session cannot re-open entries
