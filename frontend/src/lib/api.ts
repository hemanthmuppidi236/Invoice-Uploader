/**
 * API client for the Ferrocrete backend.
 *
 * Usage:
 *   import { api } from "@/lib/api";
 *   const projects = await api.get<Project[]>("/projects");
 *   const newProject = await api.post<Project>("/projects", { name, project_no });
 *
 * Auth: automatically attaches the Supabase access token from the current session.
 * Errors:
 *   - Non-2xx HTTP responses throw ApiError with .status and .detail.
 *   - Network/CORS failures throw NetworkError with a clear message and .cause.
 *   - Both extend Error and have a user-facing .message; use formatApiError()
 *     to produce a consistent string for display.
 */

import { createClient } from "@/lib/supabase/client";

const API_URL = process.env.NEXT_PUBLIC_API_URL;
if (!API_URL && typeof window !== "undefined") {
  console.error("NEXT_PUBLIC_API_URL is not set");
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
    public body?: unknown
  ) {
    super(detail);
    this.name = "ApiError";
  }
}

/**
 * Thrown when fetch() itself fails — DNS, connection drop, CORS, etc.
 * These are distinct from API errors because there's no HTTP status code
 * to display and the user usually needs to retry or check connectivity.
 */
export class NetworkError extends Error {
  constructor(message: string, public override cause?: unknown) {
    super(message);
    this.name = "NetworkError";
  }
}

/**
 * Produce a user-facing error string for either error type.
 * Use this in every catch block instead of String(e) so the user always
 * sees something meaningful instead of "TypeError: Failed to fetch".
 */
export function formatApiError(e: unknown): string {
  if (e instanceof ApiError) {
    // 500-level: show "Server error: <detail>" rather than just the number.
    // 4xx: show the detail directly since it's usually meaningful.
    if (e.status >= 500) return `Server error (${e.status}): ${e.detail}`;
    if (e.status === 401) return "You're signed out. Refresh and sign in again.";
    if (e.status === 403) return `Not allowed: ${e.detail}`;
    if (e.status === 404) return `Not found: ${e.detail}`;
    if (e.status === 409) return `Conflict: ${e.detail}`;
    if (e.status === 413) return "File too large for upload.";
    if (e.status === 422) return `Invalid input: ${e.detail}`;
    return `${e.status}: ${e.detail}`;
  }
  if (e instanceof NetworkError) {
    return `Couldn't reach the server. ${e.message}`;
  }
  if (e instanceof Error) {
    return e.message || String(e);
  }
  return String(e);
}

type Method = "GET" | "POST" | "PATCH" | "PUT" | "DELETE";

interface RequestOptions {
  signal?: AbortSignal;
  /** Send body as multipart/form-data instead of JSON. Used for file uploads. */
  formData?: FormData;
}

async function getAccessToken(): Promise<string | null> {
  const supabase = createClient();
  const {
    data: { session },
  } = await supabase.auth.getSession();
  return session?.access_token ?? null;
}

async function request<T = unknown>(
  method: Method,
  path: string,
  body?: unknown,
  options: RequestOptions = {}
): Promise<T> {
  if (!API_URL) {
    throw new ApiError(0, "API URL not configured");
  }

  const url = `${API_URL}${path}`;
  const token = await getAccessToken();

  const headers: Record<string, string> = {};
  if (token) headers.Authorization = `Bearer ${token}`;

  let payload: BodyInit | undefined;
  if (options.formData) {
    payload = options.formData; // browser sets multipart boundary header
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }

  let res: Response;
  try {
    res = await fetch(url, {
      method,
      headers,
      body: payload,
      signal: options.signal,
    });
  } catch (e) {
    // fetch() throws TypeError on network-level failures. This is the
    // class of errors that previously surfaced as "TypeError: Failed to fetch".
    // Common causes: server unreachable, CORS preflight failed, connection
    // dropped mid-request, or — sneakily — a 500 response with missing CORS
    // headers (which Chrome treats as a network error even though bytes came back).
    if (e instanceof DOMException && e.name === "AbortError") {
      throw e; // let abort propagate
    }
    const msg =
      e instanceof TypeError
        ? "The server may be cold-starting or unreachable. Wait a moment and try again."
        : `Network error: ${e instanceof Error ? e.message : String(e)}`;
    throw new NetworkError(msg, e);
  }

  // 204 No Content
  if (res.status === 204) return undefined as T;

  const text = await res.text();
  let parsed: unknown = undefined;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = text;
    }
  }

  if (!res.ok) {
    const detail =
      (parsed as { detail?: string } | undefined)?.detail ??
      (typeof parsed === "string" ? parsed : `HTTP ${res.status}`);
    throw new ApiError(res.status, detail, parsed);
  }

  return parsed as T;
}

/** Fetch a binary file (e.g. an .xlsx export) with auth, as a Blob. */
async function getBlob(path: string): Promise<Blob> {
  if (!API_URL) throw new ApiError(0, "API URL not configured");
  const token = await getAccessToken();
  const headers: Record<string, string> = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, { headers });
  } catch (e) {
    throw new NetworkError(
      "The server may be cold-starting or unreachable. Wait a moment and try again.",
      e
    );
  }
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiError(res.status, text || `HTTP ${res.status}`);
  }
  return res.blob();
}

export const api = {
  get<T = unknown>(path: string, opts?: RequestOptions) {
    return request<T>("GET", path, undefined, opts);
  },
  getBlob,
  post<T = unknown>(path: string, body?: unknown, opts?: RequestOptions) {
    return request<T>("POST", path, body, opts);
  },
  patch<T = unknown>(path: string, body?: unknown, opts?: RequestOptions) {
    return request<T>("PATCH", path, body, opts);
  },
  put<T = unknown>(path: string, body?: unknown, opts?: RequestOptions) {
    return request<T>("PUT", path, body, opts);
  },
  delete<T = unknown>(path: string, opts?: RequestOptions) {
    return request<T>("DELETE", path, undefined, opts);
  },
};
