import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { api, data } from "../api/client";
import { VerificationProgress } from "../components/Verification";
import { QueryView, useTitle } from "../components/ui";

/** A verification started from a build session or a commit: its release is known once the
 * worker has packaged the commit. */
export function VerificationPage() {
  const id = Number(useParams().verificationId);
  const query = useQuery({
    queryKey: ["verification", id],
    queryFn: async () => data(await api.GET("/api/v1/verifications/{verification_id}", { params: { path: { verification_id: id } } })),
  });
  useTitle(`Verification #${id}`);
  return (
    <QueryView query={query}>{(v) => (
      <>
        <h1>Verification #{v.id} {v.commit_sha && <code>{v.commit_sha.slice(0, 12)}</code>}</h1>
        <VerificationProgress initial={v} showRelease />
      </>
    )}</QueryView>
  );
}
