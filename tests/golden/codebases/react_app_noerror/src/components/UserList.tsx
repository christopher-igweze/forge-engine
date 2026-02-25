import React, { useEffect, useState } from "react";

export default function UserList() {
  const [users, setUsers] = useState([]);

  useEffect(() => {
    // No error handling, no loading state
    fetch("/api/users")
      .then((res) => res.json())
      .then(setUsers);
  }, []);

  // No loading indicator — renders empty immediately
  return (
    <div>
      <h2>Users</h2>
      <ul>
        {users.map((user: any) => (
          <li key={user.id}>{user.name}</li>
        ))}
      </ul>
    </div>
  );
}
