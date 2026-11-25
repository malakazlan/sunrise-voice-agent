import type { Metadata } from "next";

import { AppointmentsView } from "./appointments";

export const metadata: Metadata = { title: "Appointments" };

export default function AppointmentsPage() {
  return <AppointmentsView />;
}
