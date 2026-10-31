from __future__ import annotations

import json
from pathlib import Path

from handy_worker import conditions, config

PMSET_AC = """Now drawing from 'AC Power'
 -InternalBattery-0 (id=1234)	100%; charged; 0:00 remaining present: true
"""
PMSET_BATT = "Now drawing from 'Battery Power'\n -InternalBattery-0\t80%; discharging;"
PMSET_G = """System-wide power settings:
Currently in use:
 standby              1
 lowpowermode         1
 sleep                1
"""
THERM_OK = "Note: No thermal warning level has been recorded\n"
THERM_HOT = (
    "CPU Power notify\n\tCPU_Scheduler_Limit \t= 100\n"
    "\tCPU_Available_CPUs \t= 8\n\tCPU_Speed_Limit \t= 70\n"
)
VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               10000.
Pages active:                            200000.
Pages inactive:                           20000.
Pages speculative:                         5000.
Pages purgeable:                           1000.
"""
IOREG = '    |   "HIDIdleTime" = 12500000000\n'


def test_parsers() -> None:
    assert conditions.parse_pmset_batt(PMSET_AC) is False
    assert conditions.parse_pmset_batt(PMSET_BATT) is True
    assert conditions.parse_pmset_batt("garbage") is None
    assert conditions.parse_low_power(PMSET_G) is True
    assert (
        conditions.parse_low_power(PMSET_G.replace("lowpowermode         1", "lowpowermode 0"))
        is False
    )
    assert conditions.parse_therm(THERM_OK) is False
    assert conditions.parse_therm(THERM_HOT) is True
    assert conditions.parse_vm_stat(VM_STAT) == (10000 + 20000 + 5000 + 1000) * 16384
    assert conditions.parse_hid_idle(IOREG) == 12.5


def test_config_defaults_and_validation(tmp_path: Path) -> None:
    assert config.load(tmp_path / "missing.json") == config.WorkerConfig()
    p = tmp_path / "config.json"
    p.write_text(
        json.dumps(
            {
                "schedule_hour": 3,
                "schedule_minute": 30,
                "skip_on_battery": False,
                "stop_by_hour": 99,  # invalid → default
                "max_attempts": "three",  # invalid → default
                "min_free_disk_gb": 2,
                "unknown_key": 1,
            }
        )
    )
    c = config.load(p)
    assert (c.schedule_hour, c.schedule_minute, c.skip_on_battery) == (3, 30, False)
    assert c.stop_by_hour == 7 and c.max_attempts == 3
    assert c.min_free_disk_gb == 2.0
    p.write_text("[not an object")
    assert config.load(p) == config.WorkerConfig()
