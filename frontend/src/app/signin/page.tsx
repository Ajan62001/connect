"use client";

import { Suspense, useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { TerminalIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuthMethods, useDevLogin, useMe } from "@/lib/queries";

/** Friendly copy for the callback's ?error= reasons (backend redirect). */
const ERROR_COPY: Record<string, string> = {
  not_invited:
    "You're not on the invite list — ask the admin for an invite, then sign in again.",
  disabled: "This account has been disabled.",
  email_unverified:
    "Google did not supply a verified email for this account.",
  oauth_failed: "Google sign-in failed — please try again.",
};

function GoogleMark() {
  // Official multicolor "G" (Google brand guidelines allow inline use).
  return (
    <svg viewBox="0 0 24 24" aria-hidden className="size-4">
      <path
        fill="#4285F4"
        d="M23.52 12.27c0-.85-.08-1.66-.22-2.45H12v4.64h6.46a5.53 5.53 0 0 1-2.4 3.62v3h3.88c2.27-2.09 3.58-5.17 3.58-8.81Z"
      />
      <path
        fill="#34A853"
        d="M12 24c3.24 0 5.96-1.07 7.94-2.91l-3.88-3.01c-1.07.72-2.45 1.15-4.06 1.15-3.13 0-5.78-2.11-6.72-4.95H1.27v3.11A12 12 0 0 0 12 24Z"
      />
      <path
        fill="#FBBC05"
        d="M5.28 14.28a7.2 7.2 0 0 1 0-4.56V6.61H1.27a12 12 0 0 0 0 10.78l4.01-3.11Z"
      />
      <path
        fill="#EA4335"
        d="M12 4.77c1.76 0 3.34.61 4.59 1.8l3.44-3.44C17.95 1.19 15.24 0 12 0A12 12 0 0 0 1.27 6.61l4.01 3.11C6.22 6.88 8.87 4.77 12 4.77Z"
      />
    </svg>
  );
}

/** useSearchParams consumer, Suspense-wrapped per the App Router contract. */
function SignInError() {
  const params = useSearchParams();
  const error = params.get("error");
  if (!error) return null;
  return (
    <p
      role="alert"
      className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive"
    >
      {ERROR_COPY[error] ?? "Sign-in failed — please try again."}
    </p>
  );
}

function SignInCard() {
  const methods = useAuthMethods();
  const devLogin = useDevLogin();
  const router = useRouter();

  // Already signed in (e.g. back-button onto /signin): straight to the app.
  // useMe's 401 interceptor is a no-op on this page by design.
  const me = useMe();
  useEffect(() => {
    if (me.data) router.replace("/");
  }, [me.data, router]);

  return (
    <Card className="w-full max-w-sm">
      <CardHeader>
        <CardTitle className="text-lg">connect</CardTitle>
        <CardDescription>
          Information intelligence for Indian policy, politics, and finance.
          Sign in to continue.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <Suspense fallback={null}>
          <SignInError />
        </Suspense>

        {methods.isPending ? (
          <Skeleton className="h-9 w-full" />
        ) : methods.isError ? (
          <p className="text-xs text-muted-foreground">
            Can&apos;t reach the connect API — is the backend running?
          </p>
        ) : (
          <>
            {methods.data.google ? (
              <Button
                variant="outline"
                size="lg"
                className="w-full"
                onClick={() => window.location.assign("/api/auth/login")}
              >
                <GoogleMark />
                Continue with Google
              </Button>
            ) : (
              <p className="rounded-lg border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
                Google sign-in isn&apos;t configured on this deployment
                (GOOGLE_OAUTH_CLIENT_ID unset — see the README).
              </p>
            )}

            {methods.data.dev ? (
              <>
                <div className="flex items-center gap-3">
                  <Separator className="flex-1" />
                  <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
                    dev only
                  </span>
                  <Separator className="flex-1" />
                </div>
                <Button
                  variant="secondary"
                  size="lg"
                  className="w-full"
                  disabled={devLogin.isPending}
                  onClick={() =>
                    devLogin.mutate(undefined, {
                      onSuccess: () => window.location.assign("/"),
                    })
                  }
                >
                  <TerminalIcon />
                  Dev login
                </Button>
                {devLogin.isError ? (
                  <p className="text-xs text-destructive" role="alert">
                    Dev login failed —{" "}
                    {devLogin.error instanceof Error
                      ? devLogin.error.message
                      : "unknown error"}
                  </p>
                ) : null}
                <p className="text-[11px] leading-snug text-muted-foreground">
                  The dev sign-in hatch is enabled because
                  CONNECT_DEV_LOGIN_EMAIL is set on the backend. Never enable
                  it in production.
                </p>
              </>
            ) : null}
          </>
        )}
      </CardContent>
    </Card>
  );
}

export default function SignInPage() {
  return (
    <div className="flex h-full items-center justify-center bg-muted/20 p-6">
      <SignInCard />
    </div>
  );
}
