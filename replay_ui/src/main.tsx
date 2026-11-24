import { createRoot } from "react-dom/client";
import App from "./App";
import { connect } from "./bridge";
import "./styles.css";

connect().then((bridge) => {
  createRoot(document.getElementById("root")!).render(<App bridge={bridge} />);
});
