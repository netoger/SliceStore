-- migrations/init.sql

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE slices (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            VARCHAR(255) NOT NULL,
    printer_id      VARCHAR(100),
    material        VARCHAR(100),
    layer_count     INTEGER,
    file_size       BIGINT,
    checksum        VARCHAR(64),
    compression     SMALLINT DEFAULT 2,
    printer_type    VARCHAR(10),
    format          VARCHAR(10) DEFAULT 'sls',
    source_file     TEXT,
    hatch_strategy  VARCHAR(20),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    deleted_at      TIMESTAMPTZ,
    storage_key     VARCHAR(512)
);

CREATE TABLE slice_versions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    slice_id        UUID NOT NULL REFERENCES slices(id) ON DELETE CASCADE,
    version_num     INTEGER NOT NULL,
    message         TEXT,
    author          VARCHAR(255),
    file_size       BIGINT,
    storage_key     VARCHAR(512),
    delta_key       VARCHAR(512),
    is_snapshot     BOOLEAN DEFAULT FALSE,
    change_type     VARCHAR(20) DEFAULT 'full',
    change_summary  TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (slice_id, version_num)
);

CREATE TABLE slice_tags (
    slice_id  UUID NOT NULL REFERENCES slices(id) ON DELETE CASCADE,
    tag       VARCHAR(100) NOT NULL,
    PRIMARY KEY (slice_id, tag)
);

CREATE TABLE gcode_layer_index (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    slice_id    UUID NOT NULL REFERENCES slices(id) ON DELETE CASCADE,
    layer_num   INTEGER NOT NULL,
    byte_offset BIGINT NOT NULL,
    z_height_mm NUMERIC(8, 4),
    UNIQUE (slice_id, layer_num)
);

CREATE INDEX idx_slices_name      ON slices(name) WHERE deleted_at IS NULL;
CREATE INDEX idx_slices_printer   ON slices(printer_id) WHERE deleted_at IS NULL;
CREATE INDEX idx_slices_created   ON slices(created_at DESC);
CREATE INDEX idx_tags_tag         ON slice_tags(tag);
CREATE INDEX idx_versions_slice   ON slice_versions(slice_id, version_num DESC);
CREATE INDEX idx_gcode_layer_slice ON gcode_layer_index(slice_id);

CREATE INDEX idx_slices_fts ON slices USING gin(
    to_tsvector('russian', coalesce(name,'') || ' ' || coalesce(material,'') || ' ' || coalesce(printer_id,''))
);
