"""ESCO outage schedule settings."""
DOMAIN = "esco_outages"
URL = "https://esco.kr.ua/outages.php"
LINES = ("1.1", "1.2", "2.1", "2.2", "3.1", "3.2", "4.1", "4.2", "5.1", "5.2", "6.1", "6.2")
INTERVALS = (5, 10, 15, 30, 60)

# Real outage detection (power entity going unavailable/unknown).
OUTAGE_DELAY = 180      # seconds the entity must stay down before an outage is confirmed
HEARTBEAT = 300         # seconds between "tracker is alive" marks, used to find gaps in coverage
GAP_AFTER = 420         # a longer silence than this (HA stopped, option disabled) is a coverage gap
RETENTION_DAYS = 60     # how long finished outages and gaps are kept
MIN_LEFTOVER = 300      # seconds: shorter bits of a real outage sticking out of a planned window are ignored

# How the monitored entity reports a power outage (option `power_mode`).
MODE_UNAVAILABLE = "unavailable"  # the device goes unavailable/unknown without power, e.g. a Wi-Fi relay
MODE_SWITCH_ON = "switch_on"      # an explicit flag: on = outage, off = power present; unavailable = gap
POWER_MODES = (MODE_UNAVAILABLE, MODE_SWITCH_ON)
