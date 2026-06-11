-- Add missing columns expected by Google ADK (google-adk==1.12.0+) for the `events` table.
-- Safe to run multiple times.

ALTER TABLE "events" ADD COLUMN IF NOT EXISTS "custom_metadata" jsonb;
ALTER TABLE "events" ADD COLUMN IF NOT EXISTS "usage_metadata" jsonb;
ALTER TABLE "events" ADD COLUMN IF NOT EXISTS "citation_metadata" jsonb;
ALTER TABLE "events" ADD COLUMN IF NOT EXISTS "input_transcription" jsonb;
ALTER TABLE "events" ADD COLUMN IF NOT EXISTS "output_transcription" jsonb;

