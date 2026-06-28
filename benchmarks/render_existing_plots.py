from pathlib import Path

import pandas as pd

import generate_experiment_assets as gen


def main() -> None:
    base = Path("benchmark_artifacts")
    size_rows = pd.read_csv(base / "size_benchmark.csv").to_dict("records")
    delta_rows = pd.read_csv(base / "delta_benchmark.csv").to_dict("records")
    gen.render_plots(base, size_rows, delta_rows)
    gen.write_summary(base, size_rows, delta_rows)
    print("plots_ready")


if __name__ == "__main__":
    main()
