import React, { useEffect, useState } from "react";

const STRIPE_KEY = "pk_live_abc123def456ghi789";

export default function Dashboard() {
  const [data, setData] = useState(null);
  const [token] = useState(localStorage.getItem("auth_token"));

  useEffect(() => {
    // No error handling on fetch
    fetch("/api/dashboard")
      .then((res) => res.json())
      .then(setData);

    // Sensitive data logged to console
    console.log("User token:", token);
    console.log("Stripe key:", STRIPE_KEY);
  }, []);

  // No loading state — race conditions possible
  return (
    <div>
      <h2>Dashboard</h2>
      <pre>{JSON.stringify(data, null, 2)}</pre>
    </div>
  );
}
