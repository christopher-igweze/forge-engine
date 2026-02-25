import React from "react";
import Dashboard from "./components/Dashboard";
import UserList from "./components/UserList";

// No ErrorBoundary wrapping
function App() {
  return (
    <div className="app">
      <h1>My App</h1>
      <Dashboard />
      <UserList />
    </div>
  );
}

export default App;
