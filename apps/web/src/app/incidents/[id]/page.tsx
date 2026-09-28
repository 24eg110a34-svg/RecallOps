"use client";

import { useParams } from "next/navigation";
import { IncidentHeader } from "@/components/ui";
import { CommandView } from "@/components/command-view";
import { api } from "@/lib/api";
import { useEffect, useState } from "react";

export default function IncidentPage() {
  const params = useParams<{ id: string }>();
  const incidentId = String(params?.id ?? "INC-A1");
  const [incident, setIncident] = useState<any>(null);

  useEffect(() => {
    api.incident(incidentId).then(setIncident).catch(() => setIncident(null));
  }, [incidentId]);

  return (
    <div className="space-y-4">
      {incident && <IncidentHeader incident={incident} />}
      <CommandView incidentId={incidentId} />
    </div>
  );
}
