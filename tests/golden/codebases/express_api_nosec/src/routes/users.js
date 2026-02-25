const express = require("express");
const router = express.Router();
const { Pool } = require("pg");

const pool = new Pool({ connectionString: process.env.DB_URL });

// SQL injection vulnerability — string concatenation
router.get("/search", async (req, res) => {
  try {
    const { name } = req.query;
    const result = await pool.query(
      `SELECT * FROM users WHERE name = '${name}'`
    );
    res.json(result.rows);
  } catch (err) {
    // Exposes internal error details
    res.status(500).json({ error: err.message, stack: err.stack });
  }
});

router.get("/:id", async (req, res) => {
  const result = await pool.query("SELECT * FROM users WHERE id = $1", [req.params.id]);
  res.json(result.rows[0]);
});

router.post("/", async (req, res) => {
  // No input validation
  const { name, email, password } = req.body;
  const result = await pool.query(
    "INSERT INTO users (name, email, password) VALUES ($1, $2, $3) RETURNING *",
    [name, email, password]
  );
  res.status(201).json(result.rows[0]);
});

module.exports = router;
