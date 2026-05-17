import { Link, useLocation } from "react-router-dom";
import {
  LayoutDashboard,
  BookOpen,
  FileText,
  Users,
  ListChecks,
  Rocket,
} from "lucide-react";
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar";

const NAV = [
  { title: "总览", url: "/dashboard", icon: LayoutDashboard },
  // 专题作为一等公民：置顶在"账号"/"文章"之前
  { title: "专题", url: "/topics", icon: BookOpen },
  { title: "账号", url: "/accounts", icon: Users },
  { title: "文章", url: "/articles", icon: FileText },
  { title: "任务", url: "/jobs", icon: ListChecks },
];

export function AppSidebar() {
  const { pathname } = useLocation();

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader>
        <div className="flex items-center gap-2 px-2 py-2">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            <Rocket className="size-4" />
          </div>
          <div className="flex flex-col text-sm leading-tight">
            <span className="font-semibold">ai-ops-auto</span>
            <span className="text-xs text-muted-foreground">运营自动化</span>
          </div>
        </div>
      </SidebarHeader>
      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>管理</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {NAV.map((item) => {
                const isActive =
                  pathname === item.url ||
                  (item.url !== "/" && pathname.startsWith(item.url + "/"));
                return (
                  <SidebarMenuItem key={item.url}>
                    <SidebarMenuButton asChild isActive={isActive}>
                      <Link to={item.url}>
                        <item.icon />
                        <span>{item.title}</span>
                      </Link>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                );
              })}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>
    </Sidebar>
  );
}
