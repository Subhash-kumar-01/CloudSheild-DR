// No inline handlers anywhere in the templates (keeps the CSP strict).
document.addEventListener("submit", function (event) {
  var message = event.target.getAttribute("data-confirm");
  if (message && !window.confirm(message)) event.preventDefault();
});

document.addEventListener("change", function (event) {
  var select = event.target;
  if (select.matches && select.matches("select[data-form-action]") && select.form) {
    select.form.action = select.value; // option values are the target URLs
  }
});

document.addEventListener("click", function (event) {
  var button = event.target.closest("[data-copy]");
  if (!button) return;
  var source = document.querySelector(button.getAttribute("data-copy"));
  if (!source || !navigator.clipboard) return;
  navigator.clipboard.writeText(source.value || source.textContent).then(function () {
    var original = button.textContent;
    button.textContent = "Copied";
    setTimeout(function () { button.textContent = original; }, 1500);
  });
});

// Pages with running jobs reload themselves until the work finishes.
(function () {
  var marker = document.querySelector("[data-autorefresh]");
  if (marker) {
    setTimeout(function () { window.location.reload(); },
               parseInt(marker.getAttribute("data-autorefresh"), 10) * 1000);
  }
})();
