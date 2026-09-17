// Documentation-site helpers shared by the Linux documentation bundle.
// The failure-code registry is intentionally kept in this file so the
// rendered documentation and the runtime diagnostics use the same identifiers.
const FTHR_FAILURE_CODES = Object.freeze({
  AUDIT_005: 'AUDIT-005',
  AUDIT_008: 'AUDIT-008',
  AUDIT_009: 'AUDIT-009',
  AUDIT_013: 'AUDIT-013',
  CAPTURE_BACKEND_UNAVAILABLE: 'CAPTURE-BACKEND-UNAVAILABLE',
  CAPTURE_START_FAILED: 'CAPTURE-START-FAILED',
  ENCODER_UNAVAILABLE: 'ENCODER-UNAVAILABLE',
  FFMPEG_UNAVAILABLE: 'FFMPEG-UNAVAILABLE',
  INVALID_CAPTURE_CONFIGURATION: 'INVALID-CAPTURE-CONFIGURATION',
  WAYLAND_UNAVAILABLE: 'WAYLAND-UNAVAILABLE',
});

function failureCode(code) {
  return FTHR_FAILURE_CODES[code] || code;
}

if (typeof window !== 'undefined') {
  window.FTHR_FAILURE_CODES = FTHR_FAILURE_CODES;
  window.failureCode = failureCode;
}
