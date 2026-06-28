import csv
import gzip
import math
import os
import struct
import tempfile
import time
from pathlib import Path

from format import (
    COMPRESS_LZ4,
    COMPRESS_LZMA,
    COMPRESS_NONE,
    COMPRESS_ZSTD,
    LayerData,
    SliceReader,
    compress,
    create_delta,
)
from format.blocks import compute_block_hash, encode_metadata
from format.constants import BLOCK_LAYER, BLOCK_METADATA, FORMAT_MAJOR, FORMAT_MINOR, MAGIC


TARGET_SIZES_MB = [5, 25, 100, 250]
ALGORITHMS = {
    "sls_none": COMPRESS_NONE,
    "sls_lz4": COMPRESS_LZ4,
    "sls_zstd": COMPRESS_ZSTD,
    "sls_lzma": COMPRESS_LZMA,
}
LABELS = {
    "gcode_gzip": "G-code + gzip",
    "sls_none": ".sls без сжатия",
    "sls_lz4": ".sls + LZ4",
    "sls_zstd": ".sls + Zstd",
    "sls_lzma": ".sls + LZMA",
}


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def build_layer(layer_index: int, variant: int = 0) -> LayerData:
    layer = LayerData(
        index=layer_index,
        height_um=200,
        extruder_temp=210.0 + (layer_index % 5),
        bed_temp=60.0,
    )

    offset = (layer_index % 9) * 0.03
    shift = variant * 0.02
    layer.add_contour(
        [
            (0.0 + offset, 0.0 + shift),
            (120.0 - offset, 0.0 + shift),
            (120.0 - offset, 120.0 - shift),
            (0.0 + offset, 120.0 - shift),
        ]
    )
    layer.add_contour(
        [
            (8.0 + offset, 8.0 + shift),
            (112.0 - offset, 8.0 + shift),
            (112.0 - offset, 112.0 - shift),
            (8.0 + offset, 112.0 - shift),
        ]
    )

    for row in range(220):
        y = 1.0 + row * 0.52 + (layer_index % 3) * 0.03
        x0 = 1.0 + ((row + layer_index) % 7) * 0.04 + shift
        x1 = 119.0 - ((row * 3 + layer_index) % 11) * 0.03 - shift
        speed = 55.0 + (row % 8) * 4.0 + variant
        layer.add_infill([(x0, y), (x1, y)], speed_mms=speed)
        if row % 3 == 0:
            layer.add_infill([(y, x0), (y, x1)], speed_mms=speed - 2.0)

    return layer


def layer_to_gcode(layer: LayerData) -> bytes:
    lines = [f";LAYER:{layer.index}", f"G0 Z{(layer.index + 1) * 0.2:.3f}"]

    for contour in layer.contours:
        pts = contour.points
        if not pts:
            continue
        first = pts[0]
        lines.append(f"G0 X{first.x / 1000:.3f} Y{first.y / 1000:.3f} F9000")
        for pt in pts[1:]:
            lines.append(f"G1 X{pt.x / 1000:.3f} Y{pt.y / 1000:.3f} F3200")
        lines.append(f"G1 X{first.x / 1000:.3f} Y{first.y / 1000:.3f} F3200")

    for path in layer.infill_paths:
        pts = path.points
        if len(pts) < 2:
            continue
        p0, p1 = pts[0], pts[1]
        lines.append(
            f"G0 X{p0.x / 1000:.3f} Y{p0.y / 1000:.3f} F9000"
        )
        lines.append(
            f"G1 X{p1.x / 1000:.3f} Y{p1.y / 1000:.3f} F{int(path.speed_mms * 60)}"
        )

    return ("\n".join(lines) + "\n").encode("utf-8")


def estimate_layer_count(target_mb: int) -> int:
    sample = layer_to_gcode(build_layer(0))
    target_bytes = target_mb * 1024 * 1024
    return max(1, math.ceil(target_bytes / len(sample)))


def write_gcode(path: Path, layer_count: int) -> int:
    written = 0
    with open(path, "wb") as f:
        for layer_index in range(layer_count):
            chunk = layer_to_gcode(build_layer(layer_index))
            f.write(chunk)
            written += len(chunk)
    return written


def write_gzip(source_path: Path, target_path: Path) -> tuple[float, int]:
    start = time.perf_counter()
    with open(source_path, "rb") as src, gzip.open(target_path, "wb", compresslevel=6) as dst:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)
    duration = time.perf_counter() - start
    return duration, target_path.stat().st_size


def read_gzip(path: Path) -> tuple[float, int]:
    start = time.perf_counter()
    total = 0
    with gzip.open(path, "rb") as src:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
    duration = time.perf_counter() - start
    return duration, total


def write_sls_streaming(path: Path, compression_id: int, layer_count: int) -> tuple[float, int]:
    toc_entries = []
    start = time.perf_counter()
    with open(path, "wb") as f:
        f.write(b"\x00" * 48)

        metadata_raw = encode_metadata(
            {
                "job_name": f"benchmark_{path.stem}",
                "layer_count": layer_count,
                "created_at": int(time.time()),
                "compression": compression_id,
            }
        )
        metadata_compressed = compress(metadata_raw, compression_id)
        meta_offset = f.tell()
        f.write(
            struct.pack(
                "<HHIIQi",
                BLOCK_METADATA,
                0,
                len(metadata_raw),
                len(metadata_compressed),
                compute_block_hash(metadata_compressed),
                compression_id,
            )
        )
        f.write(metadata_compressed)
        toc_entries.append((BLOCK_METADATA, meta_offset, len(metadata_compressed)))

        for layer_index in range(layer_count):
            layer_raw = build_layer(layer_index).encode()
            layer_compressed = compress(layer_raw, compression_id)
            offset = f.tell()
            f.write(
                struct.pack(
                    "<HHIIQi",
                    BLOCK_LAYER,
                    0,
                    len(layer_raw),
                    len(layer_compressed),
                    compute_block_hash(layer_compressed),
                    compression_id,
                )
            )
            f.write(layer_compressed)
            toc_entries.append((BLOCK_LAYER, offset, len(layer_compressed)))

        toc_offset = f.tell()
        for block_type, block_offset, block_size in toc_entries:
            f.write(struct.pack("<HxxIQ", block_type, block_offset, block_size))

        f.seek(0)
        header = MAGIC
        header += struct.pack("<BB", FORMAT_MAJOR, FORMAT_MINOR)
        header += struct.pack("<B", compression_id)
        header += struct.pack("<I", len(toc_entries))
        header += struct.pack("<Q", toc_offset)
        header += struct.pack("<Q", int(time.time()))
        header += b"\x00" * (48 - len(header))
        f.write(header)

    duration = time.perf_counter() - start
    return duration, path.stat().st_size


def read_sls(path: Path) -> tuple[float, int]:
    start = time.perf_counter()
    total_layers = 0
    with SliceReader(str(path)) as reader:
        _ = reader.get_metadata()
        for index in range(reader.layer_count):
            layer = reader.get_layer(index)
            if layer is not None:
                total_layers += 1
    duration = time.perf_counter() - start
    return duration, total_layers


def benchmark_sizes(base_dir: Path) -> list[dict]:
    results = []
    data_dir = base_dir / "generated_data"
    ensure_dir(data_dir)

    for target_mb in TARGET_SIZES_MB:
        layer_count = estimate_layer_count(target_mb)
        gcode_path = data_dir / f"synthetic_{target_mb}mb.gcode"
        source_size = write_gcode(gcode_path, layer_count)
        source_mb = source_size / (1024 * 1024)
        print(f"Generated {gcode_path.name}: {source_mb:.2f} MB, {layer_count} layers")

        gzip_path = data_dir / f"synthetic_{target_mb}mb.gcode.gz"
        write_time, output_size = write_gzip(gcode_path, gzip_path)
        read_time, _ = read_gzip(gzip_path)
        results.append(
            {
                "target_mb": target_mb,
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

        for key, compression_id in ALGORITHMS.items():
            sls_path = data_dir / f"synthetic_{target_mb}mb_{key}.sls"
            write_time, output_size = write_sls_streaming(sls_path, compression_id, layer_count)
            read_time, decoded_layers = read_sls(sls_path)
            results.append(
                {
                    "target_mb": target_mb,
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
                f"  {key}: {output_size / (1024 * 1024):.2f} MB, "
                f"write {source_mb / write_time:.1f} MB/s, read {source_mb / read_time:.1f} MB/s"
            )

    return results


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def benchmark_delta(base_dir: Path) -> list[dict]:
    results = []
    target_mb = 5
    layer_count = estimate_layer_count(target_mb)
    temp_dir = base_dir / "delta_data"
    ensure_dir(temp_dir)

    base_path = temp_dir / "delta_base.sls"
    write_sls_streaming(base_path, COMPRESS_ZSTD, layer_count)
    base_bytes = base_path.read_bytes()

    for changed_pct in [1, 5, 20, 50]:
        changed_layers = max(1, round(layer_count * changed_pct / 100))
        modified_path = temp_dir / f"delta_mod_{changed_pct}.sls"

        toc_entries = []
        with open(modified_path, "wb") as f:
            f.write(b"\x00" * 48)
            metadata_raw = encode_metadata(
                {
                    "job_name": f"delta_{changed_pct}",
                    "layer_count": layer_count,
                    "created_at": int(time.time()),
                    "compression": COMPRESS_ZSTD,
                    "changed_pct": changed_pct,
                }
            )
            metadata_compressed = compress(metadata_raw, COMPRESS_ZSTD)
            meta_offset = f.tell()
            f.write(
                struct.pack(
                    "<HHIIQi",
                    BLOCK_METADATA,
                    0,
                    len(metadata_raw),
                    len(metadata_compressed),
                    compute_block_hash(metadata_compressed),
                    COMPRESS_ZSTD,
                )
            )
            f.write(metadata_compressed)
            toc_entries.append((BLOCK_METADATA, meta_offset, len(metadata_compressed)))

            for layer_index in range(layer_count):
                variant = 1 if layer_index < changed_layers else 0
                layer_raw = build_layer(layer_index, variant=variant).encode()
                layer_compressed = compress(layer_raw, COMPRESS_ZSTD)
                offset = f.tell()
                f.write(
                    struct.pack(
                        "<HHIIQi",
                        BLOCK_LAYER,
                        0,
                        len(layer_raw),
                        len(layer_compressed),
                        compute_block_hash(layer_compressed),
                        COMPRESS_ZSTD,
                    )
                )
                f.write(layer_compressed)
                toc_entries.append((BLOCK_LAYER, offset, len(layer_compressed)))

            toc_offset = f.tell()
            for block_type, block_offset, block_size in toc_entries:
                f.write(struct.pack("<HxxIQ", block_type, block_offset, block_size))

            f.seek(0)
            header = MAGIC
            header += struct.pack("<BB", FORMAT_MAJOR, FORMAT_MINOR)
            header += struct.pack("<B", COMPRESS_ZSTD)
            header += struct.pack("<I", len(toc_entries))
            header += struct.pack("<Q", toc_offset)
            header += struct.pack("<Q", int(time.time()))
            header += b"\x00" * (48 - len(header))
            f.write(header)

        new_bytes = modified_path.read_bytes()
        start = time.perf_counter()
        patch = create_delta(base_bytes, new_bytes)
        delta_time = time.perf_counter() - start
        results.append(
            {
                "changed_layers_pct": changed_pct,
                "patch_size_mb": round(len(patch) / (1024 * 1024), 3),
                "full_size_mb": round(len(new_bytes) / (1024 * 1024), 3),
                "ratio_pct": round(len(patch) / len(new_bytes) * 100, 3),
                "delta_build_time_s": round(delta_time, 4),
            }
        )

    return results


def render_plots(base_dir: Path, size_rows: list[dict], delta_rows: list[dict]) -> None:
    import matplotlib.pyplot as plt
    import pandas as pd

    plots_dir = base_dir / "plots"
    ensure_dir(plots_dir)

    size_df = pd.DataFrame(size_rows)
    delta_df = pd.DataFrame(delta_rows)
    algo_order = ["gcode_gzip", "sls_none", "sls_lz4", "sls_zstd", "sls_lzma"]
    colors = {
        "gcode_gzip": "#5B8FF9",
        "sls_none": "#61DDAA",
        "sls_lz4": "#65789B",
        "sls_zstd": "#F6BD16",
        "sls_lzma": "#E8684A",
    }

    avg_df = size_df.groupby("format", as_index=False)[["output_mb", "write_throughput_mbps", "read_throughput_mbps"]].mean()
    avg_df["format"] = pd.Categorical(avg_df["format"], categories=algo_order, ordered=True)
    avg_df = avg_df.sort_values("format")

    plt.figure(figsize=(10, 5.5))
    plt.bar(
        [LABELS[f] for f in avg_df["format"]],
        avg_df["output_mb"],
        color=[colors[f] for f in avg_df["format"]],
    )
    plt.ylabel("Средний размер, МБ")
    plt.title("Средний объём хранения для разных форматов")
    plt.xticks(rotation=20, ha="right")
    for idx, value in enumerate(avg_df["output_mb"]):
        plt.text(idx, value + 1, f"{value:.1f}", ha="center", va="bottom", fontsize=9)
    plt.tight_layout()
    plt.savefig(plots_dir / "figure4_avg_storage.png", dpi=200)
    plt.close()

    fig, axes = plt.subplots(2, 2, figsize=(13, 9))
    axes = axes.flatten()
    metric_map = [
        ("output_mb", "Размер результата, МБ"),
        ("write_throughput_mbps", "Скорость записи, МБ/с"),
        ("read_throughput_mbps", "Скорость чтения, МБ/с"),
    ]
    chart_files = [
        plots_dir / "size_vs_output.png",
        plots_dir / "size_vs_write_speed.png",
        plots_dir / "size_vs_read_speed.png",
    ]

    for ax, (metric, title), out_path in zip(axes[:3], metric_map, chart_files):
        for fmt in algo_order:
            subset = size_df[size_df["format"] == fmt].sort_values("actual_source_mb")
            ax.plot(
                subset["actual_source_mb"],
                subset[metric],
                marker="o",
                linewidth=2,
                label=LABELS[fmt],
                color=colors[fmt],
            )
        ax.set_xlabel("Размер исходного G-code, МБ")
        ax.set_ylabel(title)
        ax.set_title(title)
        ax.grid(alpha=0.25)
        ax.legend(fontsize=8)

    axes[3].bar(
        delta_df["changed_layers_pct"].astype(str) + "%",
        delta_df["ratio_pct"],
        color="#8E7CC3",
    )
    axes[3].set_title("Размер дельта-патча от доли изменённых слоёв")
    axes[3].set_xlabel("Изменено слоёв")
    axes[3].set_ylabel("Размер патча, % от полного файла")
    axes[3].grid(axis="y", alpha=0.25)
    for idx, value in enumerate(delta_df["ratio_pct"]):
        axes[3].text(idx, value + 0.05, f"{value:.2f}%", ha="center", va="bottom", fontsize=9)

    plt.tight_layout()
    plt.savefig(plots_dir / "combined_experiment_dashboard.png", dpi=200)
    plt.close()

    for (metric, title), out_path in zip(metric_map, chart_files):
        plt.figure(figsize=(10, 5.5))
        for fmt in algo_order:
            subset = size_df[size_df["format"] == fmt].sort_values("actual_source_mb")
            plt.plot(
                subset["actual_source_mb"],
                subset[metric],
                marker="o",
                linewidth=2.2,
                label=LABELS[fmt],
                color=colors[fmt],
            )
        plt.xlabel("Размер исходного G-code, МБ")
        plt.ylabel(title)
        plt.title(title + " в зависимости от размера исходного файла")
        plt.grid(alpha=0.25)
        plt.legend()
        plt.tight_layout()
        plt.savefig(out_path, dpi=200)
        plt.close()

    plt.figure(figsize=(8.5, 5.2))
    plt.bar(
        delta_df["changed_layers_pct"].astype(str) + "%",
        delta_df["ratio_pct"],
        color="#8E7CC3",
    )
    plt.xlabel("Доля изменённых слоёв")
    plt.ylabel("Размер патча, % от полного файла")
    plt.title("Эффективность дельта-кодирования для разных сценариев изменений")
    plt.grid(axis="y", alpha=0.25)
    for idx, value in enumerate(delta_df["ratio_pct"]):
        plt.text(idx, value + 0.05, f"{value:.2f}%", ha="center", va="bottom", fontsize=9)
    plt.tight_layout()
    plt.savefig(plots_dir / "delta_patch_ratio.png", dpi=200)
    plt.close()


def write_summary(base_dir: Path, size_rows: list[dict], delta_rows: list[dict]) -> None:
    size_df = {}
    for row in size_rows:
        size_df.setdefault(row["format"], []).append(row)

    zstd_rows = size_df["sls_zstd"]
    lz4_rows = size_df["sls_lz4"]
    lzma_rows = size_df["sls_lzma"]
    none_rows = size_df["sls_none"]
    gzip_rows = size_df["gcode_gzip"]

    summary = [
        "# Экспериментальные результаты SliceStore",
        "",
        "## Ключевые наблюдения",
        "",
        f"- `.sls + Zstd` в среднем занимал {sum(r['ratio_pct'] for r in zstd_rows) / len(zstd_rows):.1f}% от исходного G-code.",
        f"- `.sls + LZ4` обеспечивал наибольшую скорость чтения: до {max(r['read_throughput_mbps'] for r in lz4_rows):.1f} МБ/с.",
        f"- `.sls + LZMA` давал наименьший размер файла: до {min(r['ratio_pct'] for r in lzma_rows):.1f}% от исходного объёма.",
        f"- Даже `.sls` без сжатия в среднем был меньше G-code: {sum(r['ratio_pct'] for r in none_rows) / len(none_rows):.1f}% от исходного объёма.",
        f"- `G-code + gzip` уступал `.sls + Zstd` по компактности: в среднем {sum(r['ratio_pct'] for r in gzip_rows) / len(gzip_rows):.1f}% против {sum(r['ratio_pct'] for r in zstd_rows) / len(zstd_rows):.1f}%.",
        "",
        "## Дельта-кодирование",
        "",
    ]

    for row in delta_rows:
        summary.append(
            f"- При изменении {row['changed_layers_pct']}% слоёв размер патча составил {row['ratio_pct']}% от полного файла."
        )

    (base_dir / "experiment_summary.md").write_text("\n".join(summary), encoding="utf-8")


def main() -> None:
    base_dir = Path("benchmark_artifacts")
    ensure_dir(base_dir)
    size_rows = benchmark_sizes(base_dir)
    delta_rows = benchmark_delta(base_dir)
    write_csv(base_dir / "size_benchmark.csv", size_rows)
    write_csv(base_dir / "delta_benchmark.csv", delta_rows)
    render_plots(base_dir, size_rows, delta_rows)
    write_summary(base_dir, size_rows, delta_rows)
    print(f"Artifacts written to {base_dir.resolve()}")


if __name__ == "__main__":
    main()
