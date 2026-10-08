(() => {
    "use strict";
    const source = document.getElementById("operating-chart-data");
    if (!source) return;
    const data = JSON.parse(source.textContent);
    const locale = document.documentElement.lang || "en";
    const tickMoney = new Intl.NumberFormat(locale, {style: "currency", currency: "EUR", maximumFractionDigits: 0});
    document.querySelectorAll("[data-operating-chart]").forEach(panel => {
        const chart = data[panel.dataset.operatingChart];
        const canvas = panel.querySelector("canvas");
        const tooltip = panel.querySelector(".operating-chart-tooltip");
        const hidden = new Set();
        let geometry;
        chart.series.forEach(series => {
            const button = document.createElement("button");
            button.type = "button";
            button.setAttribute("aria-pressed", "true");
            const swatch = document.createElement("span");
            swatch.style.setProperty("--series-color", series.color);
            button.append(swatch, document.createTextNode(series.label));
            button.addEventListener("click", () => {
                hidden.has(series.key) ? hidden.delete(series.key) : hidden.add(series.key);
                button.setAttribute("aria-pressed", String(!hidden.has(series.key)));
                tooltip.hidden = true;
                draw();
            });
            panel.querySelector(".operating-chart-legend").append(button);
        });
        function draw() {
            const width = Math.max(1, canvas.parentElement.clientWidth);
            const height = 280;
            const ratio = window.devicePixelRatio || 1;
            canvas.width = Math.round(width * ratio);
            canvas.height = Math.round(height * ratio);
            canvas.style.width = width + "px";
            canvas.style.height = height + "px";
            const ctx = canvas.getContext("2d");
            ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
            const visible = chart.series.filter(series => !hidden.has(series.key));
            // Number values are only drawing coordinates; exact amounts are server-formatted.
            const values = [0, ...visible.flatMap(series => series.values.map(Number))];
            let low = Math.min(...values);
            let high = Math.max(...values);
            if (low === high) high = low + 1;
            const left = Math.min(90, width * 0.28), right = 28, top = 14, bottom = 42;
            const plotWidth = Math.max(1, width - left - right);
            const plotHeight = height - top - bottom;
            const x = index => left + index / (chart.labels.length - 1) * plotWidth;
            const y = value => top + (high - value) / (high - low) * plotHeight;
            geometry = {left, plotWidth};
            ctx.clearRect(0, 0, width, height);
            ctx.font = "11px system-ui, sans-serif";
            for (let index = 0; index <= 4; index++) {
                const value = high - (high - low) * index / 4;
                const position = y(value);
                ctx.beginPath();
                ctx.strokeStyle = "#e2e8f0";
                ctx.lineWidth = 1;
                ctx.moveTo(left, position); ctx.lineTo(width - right, position); ctx.stroke();
                ctx.fillStyle = "#64748b"; ctx.textAlign = "right";
                ctx.fillText(tickMoney.format(value), left - 8, position + 4, left - 10);
            }
            ctx.beginPath(); ctx.strokeStyle = "#94a3b8";
            ctx.moveTo(left, y(0)); ctx.lineTo(width - right, y(0)); ctx.stroke();
            const step = width < 600 ? 3 : 2;
            chart.labels.forEach((label, index) => {
                if (index % step !== 0 && index !== chart.labels.length - 1) return;
                ctx.fillStyle = "#64748b"; ctx.textAlign = "center";
                ctx.fillText(label, x(index), height - 16);
            });
            visible.forEach(series => {
                ctx.beginPath(); ctx.strokeStyle = series.color; ctx.lineWidth = 2.5;
                series.values.forEach((value, index) => {
                    index === 0 ? ctx.moveTo(x(index), y(Number(value))) : ctx.lineTo(x(index), y(Number(value)));
                });
                ctx.stroke();
                series.values.forEach((value, index) => {
                    ctx.beginPath(); ctx.fillStyle = series.color;
                    ctx.arc(x(index), y(Number(value)), 3, 0, Math.PI * 2); ctx.fill();
                });
            });
        }
        canvas.addEventListener("pointermove", event => {
            if (!geometry) return;
            const coordinate = event.clientX - canvas.getBoundingClientRect().left;
            const index = Math.max(0, Math.min(chart.labels.length - 1,
                Math.round((coordinate - geometry.left) / geometry.plotWidth * (chart.labels.length - 1))));
            const heading = document.createElement("strong");
            heading.textContent = chart.labels[index];
            tooltip.replaceChildren(heading);
            chart.series.filter(series => !hidden.has(series.key)).forEach(series => {
                const line = document.createElement("div");
                line.textContent = series.label + ": " + series.formatted[index];
                tooltip.append(line);
            });
            tooltip.hidden = false;
        });
        canvas.addEventListener("pointerleave", () => { tooltip.hidden = true; });
        let timer;
        window.addEventListener("resize", () => {
            clearTimeout(timer);
            timer = setTimeout(draw, 120);
        });
        draw();
    });
})();
