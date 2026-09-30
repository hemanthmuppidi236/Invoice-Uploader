/**
 * Reading Supabase configuration out of the environment, defensively.
 *
 * A JWT copied out of the Supabase dashboard routinely arrives with invisible
 * companions: a trailing newline, a non-breaking space, a zero-width space,
 * or — the one that cost a debugging session — U+2028 LINE SEPARATOR. None of
 * them are visible in the Vercel settings UI, and none break anything until
 * the value is put into an HTTP header, at which point the runtime refuses it:
 *
 *     Cannot convert argument to a ByteString because the character at
 *     index 215 has a value of 8232 which is greater than 255.
 *
 * That message names neither the variable nor the character, and it surfaces
 * at the OAuth callback rather than at startup, so it reads like a broken
 * sign-in rather than a bad paste.
 *
 * Neither a Supabase URL nor a JWT ever legitimately contains whitespace, so
 * stripping all of it is safe and turns a class of unreadable failures into
 * a value that simply works.
 */

// Every Unicode whitespace and separator character, not just the ASCII ones.
// U+2028 and U+FEFF are the two that survive a copy-paste unnoticed.
const INVISIBLE =
  /[\s\u00a0\u1680\u2000-\u200b\u2028\u2029\u202f\u205f\u3000\ufeff]/g;

function read(value: string | undefined, name: string): string {
  const cleaned = (value ?? "").replace(INVISIBLE, "");
  if (!cleaned) {
    throw new Error(
      `${name} is not set. Add it in Vercel → Settings → Environment ` +
        `Variables, then REDEPLOY — NEXT_PUBLIC_ values are baked in at ` +
        `build time, so saving one on its own changes nothing.`
    );
  }
  return cleaned;
}

export function supabaseUrl(): string {
  return read(process.env.NEXT_PUBLIC_SUPABASE_URL, "NEXT_PUBLIC_SUPABASE_URL");
}

export function supabaseAnonKey(): string {
  return read(
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY,
    "NEXT_PUBLIC_SUPABASE_ANON_KEY"
  );
}
