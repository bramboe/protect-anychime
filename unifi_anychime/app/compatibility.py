"""Reported UniFi compatibility profile; never a claim of installed vendor code."""
SUPPORTED_PROFILES = ("1.3.7", "1.7.20")
DEFAULT_PROFILE = "1.7.20"


def profile_version(options):
    version = options.get("unifi_compatibility_profile", DEFAULT_PROFILE)
    if version not in SUPPORTED_PROFILES:
        raise ValueError("Choose an available UniFi compatibility profile.")
    # Retire the old advertised profile, including persisted installations.
    return DEFAULT_PROFILE
