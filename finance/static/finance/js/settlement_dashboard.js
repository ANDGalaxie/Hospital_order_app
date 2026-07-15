(function () {
    "use strict";

    const dataElement = document.getElementById(
        "finance-chart-data"
    );

    if (!dataElement) {
        return;
    }

    let chartData;

    try {
        chartData = JSON.parse(
            dataElement.textContent
        );
    } catch (error) {
        console.error(
            "Unable to parse finance chart data.",
            error
        );
        return;
    }

    function formatMoney(value) {
        const numericValue = Number(
            value || 0
        );

        return new Intl.NumberFormat(
            "fr-FR",
            {
                style: "currency",
                currency: "EUR",
                maximumFractionDigits: 0,
            }
        ).format(numericValue);
    }

    function getCanvasContext(canvas) {
        const ratio = (
            window.devicePixelRatio
            || 1
        );

        const width = Math.max(
            canvas.parentElement.clientWidth,
            320
        );

        const height = 280;

        canvas.width = width * ratio;
        canvas.height = height * ratio;

        canvas.style.width = `${width}px`;
        canvas.style.height = `${height}px`;

        const context = canvas.getContext(
            "2d"
        );

        context.setTransform(
            ratio,
            0,
            0,
            ratio,
            0,
            0
        );

        return {
            context,
            width,
            height,
        };
    }

    function drawEmptyChart(
        context,
        width,
        height
    ) {
        context.clearRect(
            0,
            0,
            width,
            height
        );

        context.fillStyle = "#94a3b8";
        context.font = (
            "13px system-ui, sans-serif"
        );
        context.textAlign = "center";

        context.fillText(
            "暂无趋势数据",
            width / 2,
            height / 2
        );
    }

    function drawLineChart(
        canvasId,
        source,
        series
    ) {
        const canvas = document.getElementById(
            canvasId
        );

        if (!canvas) {
            return;
        }

        const labels = source.labels || [];

        const canvasInfo = getCanvasContext(
            canvas
        );

        const context = canvasInfo.context;
        const width = canvasInfo.width;
        const height = canvasInfo.height;

        if (!labels.length) {
            drawEmptyChart(
                context,
                width,
                height
            );
            return;
        }

        const padding = {
            top: 42,
            right: 22,
            bottom: 48,
            left: 72,
        };

        const plotWidth = (
            width
            - padding.left
            - padding.right
        );

        const plotHeight = (
            height
            - padding.top
            - padding.bottom
        );

        const allValues = [0];

        series.forEach((item) => {
            const values = (
                source[item.key]
                || []
            );

            values.forEach((value) => {
                allValues.push(
                    Number(value || 0)
                );
            });
        });

        let minimum = Math.min(
            ...allValues
        );

        let maximum = Math.max(
            ...allValues
        );

        if (minimum === maximum) {
            maximum += 1;
        }

        const range = maximum - minimum;

        function xPosition(index) {
            if (labels.length === 1) {
                return (
                    padding.left
                    + plotWidth / 2
                );
            }

            return (
                padding.left
                + (
                    index
                    / (labels.length - 1)
                ) * plotWidth
            );
        }

        function yPosition(value) {
            return (
                padding.top
                + (
                    maximum - value
                ) / range * plotHeight
            );
        }

        context.clearRect(
            0,
            0,
            width,
            height
        );

        context.font = (
            "11px system-ui, sans-serif"
        );

        context.lineWidth = 1;

        const gridLines = 4;

        for (
            let index = 0;
            index <= gridLines;
            index += 1
        ) {
            const ratio = (
                index / gridLines
            );

            const y = (
                padding.top
                + ratio * plotHeight
            );

            const value = (
                maximum
                - ratio * range
            );

            context.beginPath();
            context.strokeStyle = "#e2e8f0";

            context.moveTo(
                padding.left,
                y
            );

            context.lineTo(
                width - padding.right,
                y
            );

            context.stroke();

            context.fillStyle = "#64748b";
            context.textAlign = "right";

            context.fillText(
                formatMoney(value),
                padding.left - 9,
                y + 4
            );
        }

        const zeroY = yPosition(0);

        if (
            zeroY >= padding.top
            && zeroY <= (
                padding.top + plotHeight
            )
        ) {
            context.beginPath();
            context.strokeStyle = "#94a3b8";
            context.lineWidth = 1.5;

            context.moveTo(
                padding.left,
                zeroY
            );

            context.lineTo(
                width - padding.right,
                zeroY
            );

            context.stroke();
        }

        const labelStep = Math.max(
            1,
            Math.ceil(labels.length / 8)
        );

        labels.forEach(
            (label, index) => {
                if (
                    index % labelStep !== 0
                    && index
                    !== labels.length - 1
                ) {
                    return;
                }

                context.fillStyle = "#64748b";
                context.textAlign = "center";

                context.fillText(
                    label,
                    xPosition(index),
                    height - 18
                );
            }
        );

        series.forEach((item) => {
            const values = (
                source[item.key]
                || []
            );

            if (!values.length) {
                return;
            }

            context.beginPath();
            context.strokeStyle = item.color;
            context.lineWidth = 2.5;
            context.lineJoin = "round";
            context.lineCap = "round";

            values.forEach(
                (rawValue, index) => {
                    const value = Number(
                        rawValue || 0
                    );

                    const x = xPosition(index);
                    const y = yPosition(value);

                    if (index === 0) {
                        context.moveTo(x, y);
                    } else {
                        context.lineTo(x, y);
                    }
                }
            );

            context.stroke();

            values.forEach(
                (rawValue, index) => {
                    const value = Number(
                        rawValue || 0
                    );

                    const x = xPosition(index);
                    const y = yPosition(value);

                    context.beginPath();
                    context.fillStyle = item.color;

                    context.arc(
                        x,
                        y,
                        3.5,
                        0,
                        Math.PI * 2
                    );

                    context.fill();
                }
            );
        });

        let legendX = padding.left;

        series.forEach((item) => {
            context.fillStyle = item.color;

            context.fillRect(
                legendX,
                13,
                14,
                3
            );

            context.fillStyle = "#475569";
            context.textAlign = "left";

            context.fillText(
                item.label,
                legendX + 20,
                18
            );

            legendX += (
                context.measureText(
                    item.label
                ).width
                + 58
            );
        });
    }

    function renderCharts() {
        drawLineChart(
            "accrual-finance-chart",
            chartData.accrual || {},
            [
                {
                    key: "sales",
                    label: "销售额",
                    color: "#2563eb",
                },
                {
                    key: "purchases",
                    label: "采购额",
                    color: "#f97316",
                },
                {
                    key: "gross_profit",
                    label: "预计毛利润",
                    color: "#16a34a",
                },
            ]
        );

        drawLineChart(
            "cash-finance-chart",
            chartData.cash || {},
            [
                {
                    key: "receipts",
                    label: "医院收款",
                    color: "#0f766e",
                },
                {
                    key: "payments",
                    label: "工厂付款",
                    color: "#dc2626",
                },
                {
                    key: "net_inflow",
                    label: "现金净流入",
                    color: "#7c3aed",
                },
            ]
        );
    }

    let resizeTimer = null;

    window.addEventListener(
        "resize",
        function () {
            window.clearTimeout(
                resizeTimer
            );

            resizeTimer = window.setTimeout(
                renderCharts,
                120
            );
        }
    );

    renderCharts();
}());
