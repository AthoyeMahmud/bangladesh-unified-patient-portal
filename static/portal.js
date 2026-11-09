document.querySelectorAll("[data-print]").forEach(button => {
  button.addEventListener("click", () => window.print());
});

const reportChoices = document.querySelector("[data-report-limit]");
if (reportChoices) {
  const boxes = Array.from(reportChoices.querySelectorAll('input[name="report"]'));
  const limit = Number(reportChoices.dataset.reportLimit);
  const status = reportChoices.querySelector("[data-report-count]");
  function countReports() {
    const count = boxes.filter(box => box.checked).length;
    status.textContent = count + " of " + limit + " reports selected";
    for (const box of boxes) box.disabled = !box.checked && count >= limit;
  }
  reportChoices.addEventListener("change", countReports);
  countReports();
}

const order = document.querySelector("#order");
const bed = document.querySelector("#bed");
if (order && bed) {
  function filterBeds() {
    const ward = order.selectedOptions[0]?.dataset.ward;
    for (const option of bed.options) {
      if (!option.value) continue;
      option.disabled = option.hidden = option.dataset.ward !== ward;
    }
    if (bed.selectedOptions[0]?.disabled) bed.value = "";
  }
  order.addEventListener("change", filterBeds);
  filterBeds();
}
