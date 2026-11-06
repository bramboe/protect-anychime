/* Status describes confirmed Protect setup, not merely a socket connection.
   Without a Protect account the doorbell pairing cannot be read, so it shows
   only what is certain: the live connection, and a ring Protect sent. */
function AnyChimeRingTime(seconds, now = new Date()) {
  const at = new Date(seconds * 1000);
  const time = at.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  return at.toDateString() === now.toDateString() ? time : at.toLocaleDateString([], {day: 'numeric', month: 'short'}) + ' ' + time;
}
function AnyChimeStatus(chime, live, context) {
  const status = (label, tone, detail) => detail ? {label, tone, detail} : {label, tone};
  if (chime.migration_hold) return status('Migration needs review', 'warning');
  if (chime.removal_pending) return status('Removal not confirmed', 'warning');
  if (live?.state === 'offline' || chime.speaker_available === false) return status('Offline', 'warning');
  if (live?.state === 'permission_required') return status('Network permissions need attention. Check app logs.', 'warning');
  if (live?.state === 'error' || live?.state === 'stopped') return status('Setup needs attention. Check app logs.', 'warning');
  if (live && !['connected', 'awaiting_adoption'].includes(live.state)) return status('Preparing chime', 'info');
  const adopted = typeof live?.protect_adopted === 'boolean' ? live.protect_adopted : live?.adopted;
  if (!adopted) {
    if (!context.autoAdoptEnabled) return status('Adopt chime in Protect', 'warning');
    if (!context.managementReady) return status('Configure Protect account to adopt automatically', 'warning');
    if (live?.auto_adoption_failed || live?.auto_adoption_exhausted) return status('Automatic adoption needs attention. Check app logs.', 'warning');
    return status('Adopting in Protect', 'info');
  }
  if (!live?.connected) return status('Reconnecting to Protect', 'warning');
  if (!context.managementReady) {
    const rang = Number(live.last_play?.received_at);
    if (rang > 0) return status('Active as chime', 'success', 'Last rang ' + AnyChimeRingTime(rang, context.now));
    return status('Connected to Protect', 'success', 'Pair it with your doorbell in the Protect app.');
  }
  if (live.configuration_check_failed) return status('Protect configuration check unavailable. Check app logs.', 'warning');
  if (live.protect_configured === true) return status('Active as chime', 'success');
  if (live.protect_configured === false) return status('Configure chime in Protect', 'warning');
  return status('Checking configuration in Protect', 'info');
}
