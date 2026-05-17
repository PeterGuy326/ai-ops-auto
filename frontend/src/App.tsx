import { Routes, Route, Navigate } from "react-router-dom";
import { MainLayout } from "@/components/layout/main-layout";
import Dashboard from "@/pages/dashboard";
import Topics from "@/pages/topics";
import TopicDetail from "@/pages/topic-detail";
import Articles from "@/pages/articles";
import Accounts from "@/pages/accounts";
import Jobs from "@/pages/jobs";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<MainLayout />}>
        <Route index element={<Navigate to="/dashboard" replace />} />
        <Route path="dashboard" element={<Dashboard />} />
        <Route path="topics" element={<Topics />} />
        <Route path="topics/:id" element={<TopicDetail />} />
        <Route path="articles" element={<Articles />} />
        <Route path="accounts" element={<Accounts />} />
        <Route path="jobs" element={<Jobs />} />
      </Route>
    </Routes>
  );
}
