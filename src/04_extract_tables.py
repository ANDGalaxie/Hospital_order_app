import pdfplumber
import pandas as pd
import re

pdf_path = "data/List hopitaux Acoeurs.pdf"
output_path = "outputs/list_hopitaux_acoeurs.xlsx"

rows = []

with pdfplumber.open(pdf_path) as pdf:
    for page in pdf.pages:
        tables = page.extract_tables()

        for table in tables:
            for row in table:
                if len(row) < 3:
                    continue

                hospital_name = (row[1] or "").strip()
                address = (row[2] or "").strip()

                # 跳过空行
                if not hospital_name and not address:
                    continue

                # 跳过表头
                if hospital_name.lower() in ["etablissement", "établissement"]:
                    continue

                # 清理医院名称中的多余空格
                hospital_name = re.sub(r"\s+", " ", hospital_name)

                # 保留地址中的换行，但清理每行多余空格
                address = "\n".join(
                    re.sub(r"\s+", " ", line).strip()
                    for line in address.splitlines()
                    if line.strip()
                )

                rows.append([hospital_name, address])

df = pd.DataFrame(rows, columns=["Hospital name", "Address"])
df.to_excel(output_path, index=False)

print(f"Done: {output_path}")