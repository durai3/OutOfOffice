/** Where the FastAPI backend lives. Set NEXT_PUBLIC_API_URL in web/.env.local to override. */
export const API_BASE: string = (process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");

/** WebSocket endpoint for the live meeting feed, derived from the API base. */
export const LIVE_FEED_URL: string = `${API_BASE.replace(/^http/, "ws")}/api/v1/meetings/live-feed`;

export const APP_NAME = "DELEG8";
