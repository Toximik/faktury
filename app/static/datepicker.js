// Czech date picker for every <input type="date"> (flatpickr, self-hosted in
// static/vendor/flatpickr/). The native picker formats the visible date by the
// browser's language, so an English Safari/Chrome shows 09/10/2026 even on a
// Czech-region Mac. flatpickr's altInput shows "9. 10. 2026" while the original
// input (now hidden) keeps the ISO YYYY-MM-DD value, so form posts, the API
// payloads and page scripts reading `.value` / listening for "change" are
// unaffected.
(function () {
  if (typeof flatpickr === "undefined") return;  // asset failed: keep native inputs

  // Accept typed "9.10.2026", "9. 10. 2026" and the ISO value used on load.
  function parseDate(str) {
    let m = /^\s*(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*(\d{4})\s*$/.exec(str);
    if (m) return validDate(+m[3], +m[2], +m[1]);
    m = /^\s*(\d{4})-(\d{2})-(\d{2})\s*$/.exec(str);
    if (m) return validDate(+m[1], +m[2], +m[3]);
    return undefined;
  }

  function validDate(y, mo, d) {
    const dt = new Date(y, mo - 1, d);
    return dt.getMonth() === mo - 1 && dt.getDate() === d ? dt : undefined;
  }

  flatpickr("input[type=date]", {
    locale: "cs",
    dateFormat: "Y-m-d",   // submitted value (unchanged)
    altInput: true,
    altFormat: "j. n. Y",  // what the user sees
    allowInput: true,      // typing a date works too
    disableMobile: true,   // phones would otherwise fall back to the native picker
    parseDate: parseDate,
  });
})();
