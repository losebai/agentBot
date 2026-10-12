-- Read-only compatibility catalog for dispatch_chat.model_guard.
-- Operators explicitly register model identifiers that are enabled in Java AI.
-- No provider credentials are stored here. Java AI remains authoritative.
CREATE TABLE IF NOT EXISTS ai_model (
    model_id VARCHAR(255) NOT NULL,
    type VARCHAR(32) NOT NULL,
    enabled TINYINT NOT NULL DEFAULT 0,
    PRIMARY KEY (model_id, type)
);
