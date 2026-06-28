import csv
import tempfile
from pathlib import Path

import generate_experiment_assets as gen


TARGET_MB = 1024


def main() -> None:
    host_out = Path("benchmark_artifacts_1gb")
    gen.ensure_dir(host_out)

    layer_count = gen.estimate_layer_count(TARGET_MB)

    with tempfile.TemporaryDirectory(prefix="slicestore_1gb_") as tmp:
        tmp_dir = Path(tmp)
        gcode_path = tmp_dir / "synthetic_1024mb.gcode"
        source_size = gen.write_gcode(gcode_path, layer_count)
        source_mb = source_size / (1024 * 1024)

        rows = []

        gzip_path = tmp_dir / "synthetic_1024mb.gcode.gz"
        write_time, output_size = gen.write_gzip(gcode_path, gzip_path)
        read_time, _ = gen.read_gzip(gzip_path)
        rows.append(
            {
                "target_mb": TARGET_MB,
                "actual_source_mb": round(source_mb, 2),
                "format": "gcode_gzip",
                "output_mb": round(output_size / (1024 * 1024), 2),
                "ratio_pct": round(output_size / source_size * 100, 2),
                "write_time_s": round(write_time, 4),
                "read_time_s": round(read_time, 4),
                "write_throughput_mbps": round(source_mb / write_time, 2),
                "read_throughput_mbps": round(source_mb / read_time, 2),
                "layers": layer_count,
            }
        )

        for key, compression_id in gen.ALGORITHMS.items():
            sls_path = tmp_dir / f"synthetic_1024mb_{key}.sls"
            write_time, output_size = gen.write_sls_streaming(
                sls_path, compression_id, layer_count
            )
            read_time, decoded_layers = gen.read_sls(sls_path)
            rows.append(
                {
                    "target_mb": TARGET_MB,
                    "actual_source_mb": round(source_mb, 2),
                    "format": key,
                    "output_mb": round(output_size / (1024 * 1024), 2),
                    "ratio_pct": round(output_size / source_size * 100, 2),
                    "write_time_s": round(write_time, 4),
                    "read_time_s": round(read_time, 4),
                    "write_throughput_mbps": round(source_mb / write_time, 2),
                    "read_throughput_mbps": round(source_mb / read_time, 2),
                    "layers": decoded_layers,
                }
            )
            print(
                f"{key}: {output_size / (1024 * 1024):.2f} MB, "
                f"write {source_mb / write_time:.2f} MB/s, "
                f"read {source_mb / read_time:.2f} MB/s"
            )

    out_csv = host_out / "size_benchmark_1gb.csv"
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print(f"1GB benchmark written to {out_csv.resolve()}")


if __name__ == "__main__":
    main()
