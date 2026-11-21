import { Link } from "react-router-dom";
import { useTitle } from "../components/ui";

export function NotFoundPage() {
  useTitle("Not found");
  return (
    <>
      <h1>Not found</h1>
      <p>There is no page at this address. <Link to="/">Back to the applications</Link>.</p>
    </>
  );
}
