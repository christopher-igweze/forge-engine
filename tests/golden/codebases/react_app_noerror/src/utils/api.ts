const BASE_URL = "/api";

// No error handling on any fetch calls
export async function getUsers() {
  const response = await fetch(`${BASE_URL}/users`);
  return response.json();
}

export async function getUser(id: string) {
  const response = await fetch(`${BASE_URL}/users/${id}`);
  return response.json();
}

export async function createUser(data: any) {
  const response = await fetch(`${BASE_URL}/users`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  return response.json();
}
