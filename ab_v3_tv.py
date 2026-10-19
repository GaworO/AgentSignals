"""The former one-second TV bridge is disabled for V3."""


def on_batch(body):
    # Other consumers can retain historical seconds; V3 never ingests them.
    return dict(v3_data_bridge='DISABLED_M1_ONLY',one_second_enabled=False)
