import { createRoot } from "react-dom/client";

const root = document.getElementById("root");
if (!root) throw new Error("#root missing");
createRoot(root).render(<h1>Argus</h1>);
