# Release 0.4.4 verification

325 Python tests passed with fake HA/Protect transports, plus 17 pure JavaScript lifecycle checks. New cases cover pairing evidence, missing or malformed fields, foreign/ambiguous identities, removed entries, refresh throttling, stale-success clearing, identity replacement and bounded failure logging. JavaScript syntax and source hygiene checks passed. No real playback request was sent.

The live interface showed a paired chime as green **Active as chime**, and a newly adopted unpaired chime as orange **Configure chime in Protect**. Both chimes were connected after the update, with no network setup problems reported. Browser-computed colors confirmed blue progress and the same orange for warnings, errors and dialog errors.

The preceding 0.4.3 check confirmed unchanged saved keys, members and identities relative to the pre-update snapshot. The 0.4.4 change adds only read access to the canonical Linux `/proc/<pid>/net/` path, fixing a denied route lookup when preparing an additional chime. A versioned HA update was used because Supervisor's rebuild path does not reinstall the AppArmor profile. Native chime identity, ringtone storage and membership persistence implementations were not changed.

Supervisor reported version 0.4.4, rating 6, `apparmor: profile` and `protected: true`. No acoustic alignment or audible playback was tested. No Git repository was created or pushed.

Source reference for profile reload behavior: [Supervisor app update and rebuild](https://github.com/home-assistant/supervisor/blob/main/supervisor/apps/app.py).
