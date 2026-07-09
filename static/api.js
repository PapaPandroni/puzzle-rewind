export async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch {
      // ignore non-JSON error bodies
    }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return res.json();
}
