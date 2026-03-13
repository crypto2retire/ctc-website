/* ===================================
   Clear the Clutter Junk Removal
   Shared JavaScript
   =================================== */

document.addEventListener('DOMContentLoaded', function() {

  // --- Sticky header shadow on scroll ---
  const header = document.querySelector('.site-header');
  if (header) {
    let lastScroll = 0;
    window.addEventListener('scroll', function() {
      const scrollY = window.scrollY;
      if (scrollY > 20) {
        header.classList.add('scrolled');
      } else {
        header.classList.remove('scrolled');
      }
      lastScroll = scrollY;
    }, { passive: true });
  }

  // --- Mobile nav toggle ---
  const navToggle = document.querySelector('.nav-toggle');
  const mainNav = document.querySelector('.main-nav');

  if (navToggle && mainNav) {
    navToggle.addEventListener('click', function() {
      mainNav.classList.toggle('open');
      const isOpen = mainNav.classList.contains('open');
      navToggle.innerHTML = isOpen ? '✕' : '☰';
      navToggle.setAttribute('aria-expanded', isOpen);
    });

    // Close nav when a non-dropdown link is clicked
    mainNav.querySelectorAll('a:not(.dropdown-toggle)').forEach(function(link) {
      link.addEventListener('click', function() {
        if (window.innerWidth < 1024) {
          mainNav.classList.remove('open');
          navToggle.innerHTML = '☰';
          navToggle.setAttribute('aria-expanded', 'false');
        }
      });
    });
  }

  // --- Mobile dropdown toggles ---
  document.querySelectorAll('.nav-dropdown > a').forEach(function(toggle) {
    toggle.addEventListener('click', function(e) {
      if (window.innerWidth < 1024) {
        e.preventDefault();
        const parent = this.parentElement;
        document.querySelectorAll('.nav-dropdown').forEach(function(d) {
          if (d !== parent) d.classList.remove('open');
        });
        parent.classList.toggle('open');
      }
    });
  });

  // --- Smooth scroll for anchor links ---
  document.querySelectorAll('a[href^="#"]').forEach(function(anchor) {
    anchor.addEventListener('click', function(e) {
      const targetId = this.getAttribute('href');
      if (targetId === '#') return;
      const target = document.querySelector(targetId);
      if (target) {
        e.preventDefault();
        const headerHeight = header ? header.offsetHeight : 0;
        const targetPos = target.getBoundingClientRect().top + window.scrollY - headerHeight - 20;
        window.scrollTo({ top: targetPos, behavior: 'smooth' });
      }
    });
  });

  // --- FAQ accordion ---
  document.querySelectorAll('.faq-question').forEach(function(btn) {
    btn.addEventListener('click', function() {
      const item = this.parentElement;
      const answer = item.querySelector('.faq-answer');
      const isOpen = item.classList.contains('open');

      // Close all others
      document.querySelectorAll('.faq-item.open').forEach(function(openItem) {
        if (openItem !== item) {
          openItem.classList.remove('open');
          openItem.querySelector('.faq-answer').style.maxHeight = '0';
        }
      });

      if (isOpen) {
        item.classList.remove('open');
        answer.style.maxHeight = '0';
      } else {
        item.classList.add('open');
        answer.style.maxHeight = answer.scrollHeight + 'px';
      }
    });
  });

  // --- Mobile sticky CTA show/hide on scroll ---
  const mobileCta = document.querySelector('.mobile-cta');
  if (mobileCta) {
    let lastScrollY = 0;
    window.addEventListener('scroll', function() {
      const currentScroll = window.scrollY;
      if (currentScroll > 300) {
        mobileCta.style.transform = 'translateY(0)';
      } else {
        mobileCta.style.transform = 'translateY(100%)';
      }
      lastScrollY = currentScroll;
    }, { passive: true });
    mobileCta.style.transform = 'translateY(100%)';
    mobileCta.style.transition = 'transform 0.3s ease';
  }

});
