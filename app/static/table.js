// Optional enhancement: click a header of a table.sortable to sort it, and type into
// input[data-filter-for=<table id>] to filter its rows; a select[data-autosubmit] submits its form on change.
// Pages are fully usable without this.
(function () {
  function value(row, i) {
    var cell = row.cells[i];
    if (!cell) return "";
    var v = cell.hasAttribute("data-v") ? cell.getAttribute("data-v") : cell.textContent.trim();
    var n = parseFloat(v);
    return v !== "" && !isNaN(n) && /^-?[\d.]+$/.test(v) ? n : v.toLowerCase();
  }
  document.querySelectorAll("table.sortable").forEach(function (table) {
    var body = table.tBodies[0];
    table.tHead.querySelectorAll("th").forEach(function (th, i) {
      th.addEventListener("click", function () {
        var dir = th.getAttribute("data-dir") === "1" ? -1 : 1;
        table.tHead.querySelectorAll("th").forEach(function (o) { o.removeAttribute("data-dir"); });
        th.setAttribute("data-dir", dir);
        Array.from(body.rows).sort(function (a, b) {
          var x = value(a, i), y = value(b, i);
          if (x === "" && y !== "") return 1;
          if (y === "" && x !== "") return -1;
          return (x < y ? -1 : x > y ? 1 : 0) * dir;
        }).forEach(function (r) { body.appendChild(r); });
      });
    });
  });
  document.querySelectorAll("select[data-autosubmit]").forEach(function (select) {
    select.addEventListener("change", function () { select.form.submit(); });
  });
  document.querySelectorAll("input[data-filter-for]").forEach(function (box) {
    var table = document.getElementById(box.getAttribute("data-filter-for"));
    box.addEventListener("input", function () {
      var q = box.value.toLowerCase();
      Array.from(table.tBodies[0].rows).forEach(function (r) {
        r.hidden = q !== "" && r.textContent.toLowerCase().indexOf(q) === -1;
      });
    });
  });
})();
