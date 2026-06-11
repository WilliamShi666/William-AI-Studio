-- Add role column for users and backfill existing users as admins.

ALTER TABLE "users" ADD COLUMN IF NOT EXISTS "role" varchar(20) NOT NULL DEFAULT 'user';

-- Promote all existing users to admin at migration time.
UPDATE "users" SET "role" = 'admin';

-- Ensure constraint and index exist.
ALTER TABLE "users" DROP CONSTRAINT IF EXISTS "users_role_check";
ALTER TABLE "users" ADD CONSTRAINT "users_role_check"
  CHECK ("role" IN ('admin', 'user'));

CREATE INDEX IF NOT EXISTS "idx_users_role" ON "users" USING btree ("role");

COMMENT ON COLUMN "users"."role" IS 'User role: admin, user';
