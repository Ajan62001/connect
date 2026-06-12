import { redirect } from "next/navigation";

/** /admin is a section, not a page — land on the users table. */
export default function AdminIndexPage() {
  redirect("/admin/users");
}
