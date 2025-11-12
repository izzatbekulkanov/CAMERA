var colors = ["#f1556c"];
var dataColors = $("#total-revenue").data("colors");

// HTML atributda ranglar berilgan bo‘lsa, ulardan foydalanamiz
if (dataColors) {
    colors = dataColors.split(",");
}

// Radial bar chart (aylana diagramma)
var options = {
    series: [68], // % ko‘rsatkich
    chart: {
        height: 242,
        type: "radialBar",
    },
    plotOptions: {
        radialBar: {
            hollow: {
                size: "65%", // markazdagi bo‘sh joy
            },
        },
    },
    colors: colors,
    labels: ["Revenue"], // nom
};



// =========================
//  DATE RANGE PICKER
// =========================
$("#dash-daterange").flatpickr({
    altInput: true,
    mode: "range",         // oraliq tanlash
    altFormat: "F j, Y",   // format: Masalan, “October 28, 2025”
    defaultDate: "today",  // bugungi sana
});
