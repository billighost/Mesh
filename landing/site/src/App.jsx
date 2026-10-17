import { PreferencesProvider } from "./hooks/usePreferences";
import { ToastProvider } from "./components/ui/Toast";
import CursorSpotlight from "./components/ui/CursorSpotlight";
import Topbar from "./components/Topbar";
import Hero from "./components/Hero";
import TrustStrip from "./components/TrustStrip";
import ProblemSection from "./components/ProblemSection";
import HowItWorks from "./components/HowItWorks";
import SearchExplorer from "./components/SearchExplorer";
import MemorySection from "./components/MemorySection";
import UnderTheHood from "./components/UnderTheHood";
import InterfacesSection from "./components/InterfacesSection";
import AgensiSection from "./components/AgensiSection";
import InstallSection from "./components/InstallSection";
import FaqSection from "./components/FaqSection";
import FinalCta from "./components/FinalCta";
import Footer from "./components/Footer";

export default function App() {
  return (
    <PreferencesProvider>
      <ToastProvider>
        <a className="skip-link" href="#main">Skip to content</a>
        <Topbar />
        <main id="main">
          <Hero />
          <TrustStrip />
          <ProblemSection />
          <HowItWorks />
          <SearchExplorer />
          <MemorySection />
          <UnderTheHood />
          <InterfacesSection />
          <AgensiSection />
          <InstallSection />
          <FaqSection />
          <FinalCta />
        </main>
        <Footer />
        <CursorSpotlight />
      </ToastProvider>
    </PreferencesProvider>
  );
}
