document.querySelectorAll("[data-print]").forEach(button => {
  button.addEventListener("click", () => window.print());
});

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
