"use client";

import { useEffect, useRef, useState } from "react";
import { post } from "@/lib/api";

declare global {
  interface Window {
    onTelegramAuth?: (user: Record<string, string | number>) => void;
  }
}

/** Official Telegram Login Widget. The backend verifies the HMAC signature - the browser is never trusted. */
export default function TelegramLogin({ onLogin }: { onLogin: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const bot = process.env.NEXT_PUBLIC_TELEGRAM_BOT_USERNAME ?? "KalshiAIBot";

  useEffect(() => {
    window.onTelegramAuth = async (user) => {
      try {
        await post("/api/auth/telegram", user);
        onLogin();
      } catch (e) {
        setError((e as Error).message);
      }
    };
    const script = document.createElement("script");
    script.src = "https://telegram.org/js/telegram-widget.js?22";
    script.async = true;
    script.setAttribute("data-telegram-login", bot);
    script.setAttribute("data-size", "large");
    script.setAttribute("data-onauth", "onTelegramAuth(user)");
    script.setAttribute("data-request-access", "write");
    ref.current?.appendChild(script);
    return () => {
      delete window.onTelegramAuth;
    };
  }, [bot, onLogin]);

  return (
    <div className="space-y-2">
      <div ref={ref} />
      {error && <p className="text-sm text-red-400">{error}</p>}
    </div>
  );
}
