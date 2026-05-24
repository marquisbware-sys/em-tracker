"""
Pine Script Generator
Reads expected_moves.json and produces a ready-to-paste Pine Script.
Run after calculate_em.py.
"""

import json
from datetime import datetime


def generate_pine_script(json_path="expected_moves.json", template_path="expected_move.pine.template", output_path="expected_move.pine"):
    with open(json_path, "r") as f:
        data = json.load(f)

    with open(template_path, "r") as f:
        template = f.read()

    # Build the data block
    pine_data = []
    for i, t in enumerate(data["tickers"]):
        prefix = "    if" if i == 0 else "    else if"
        weekly = t.get("weekly_em", 0)
        monthly = t.get("monthly_em", 0)
        price = t.get("price", 0)
        pine_data.append(f'{prefix} sym == "{t["ticker"]}"')
        pine_data.append(f'        weeklyEM := {weekly}')
        pine_data.append(f'        monthlyEM := {monthly}')
        pine_data.append(f'        refPrice := {price}')

    data_block = "\n".join(pine_data)

    # Replace placeholders
    start_marker = "    // [PINE_DATA_BLOCK_START]"
    end_marker = "    // [PINE_DATA_BLOCK_END]"
    before = template.split(start_marker)[0]
    after = template.split(end_marker)[1]

    final = before + start_marker + "\n" + data_block + "\n" + end_marker + after
    final = final.replace("[TIMESTAMP_PLACEHOLDER]", data["generated_at"])

    with open(output_path, "w") as f:
        f.write(final)

    print(f"Generated {output_path} with {data['count']} tickers")


if __name__ == "__main__":
    generate_pine_script()
