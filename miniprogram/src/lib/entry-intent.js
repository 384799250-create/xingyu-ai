// Short-lived launch intent only; never put a chat entry credential in storage.
let pending = '';
function receive(query) {
  if (query && typeof query.entry === 'string' && query.entry.length <= 128) pending = query.entry;
}
function take(value) { const result = typeof value === 'string' ? value : pending; pending = ''; return result; }
module.exports = { receive, take };
