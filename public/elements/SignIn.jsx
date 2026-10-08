import { useEffect, useState } from 'react';

const LAST_REDIRECT = 'opsfleet.signInRedirectAt';
const REDIRECT_COOLDOWN_MS = 60000;

function recentlyRedirected() {
  try {
    const last = Number(window.sessionStorage.getItem(LAST_REDIRECT) || 0);
    window.sessionStorage.setItem(LAST_REDIRECT, String(Date.now()));
    return Date.now() - last < REDIRECT_COOLDOWN_MS;
  } catch {
    return false;
  }
}

function signIn() {
  fetch('/logout', { method: 'POST', credentials: 'same-origin' }).finally(() => {
    window.location.assign('/login');
  });
}

export default function SignIn() {
  const [manual, setManual] = useState(false);
  useEffect(() => {
    if (recentlyRedirected()) {
      setManual(true);
      return;
    }
    signIn();
  }, []);
  if (manual) {
    return (
      <p className="text-sm">
        Sign-in did not stick.{' '}
        <button className="underline" onClick={signIn}>Sign in again</button>
      </p>
    );
  }
  return <p className="text-sm text-muted-foreground">Your session expired. Redirecting to sign-in…</p>;
}
