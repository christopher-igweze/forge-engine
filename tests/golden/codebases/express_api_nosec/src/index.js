const express = require("express");
const app = express();
const usersRouter = require("./routes/users");
const healthRouter = require("./routes/health");

app.use(express.json());
// No rate limiting
// No CORS configuration
// No helmet security headers

app.use("/api/users", usersRouter);
app.use("/api/health", healthRouter);

app.use((err, req, res, next) => {
  // Exposes full stack trace in production
  res.status(500).json({
    error: err.message,
    stack: err.stack,
  });
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log(`Server on port ${PORT}`));

module.exports = app;
